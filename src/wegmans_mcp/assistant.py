"""Wegmans' own AI shopping assistant (the "AI Assistant" button on
wegmans.com), which is Cooklist under the hood.

Captured 2026-08-01. Two channels:

1. `POST /cooklist/graphql` — `CreateStreamingChatMessage` posts the prompt
   and returns a `responseId` + `sessionId`. Notably it carries **no**
   Authorization header; the caller is identified by `userId` in the
   variables (`wgcl_<customer key>`).
2. `wss://.../websockets/prod/cooklist-shopper-assistant` — a graphql-ws
   socket that streams the answer back. This one DOES need a bearer: the
   Cooklist token from
   `GET /commerce/recipes/customer/cooklist-auth-token`.

Frames arrive as `{"type": "token", "data": "..."}` chunks, optionally a
`{"type": "processed_block", "processor": "suggested_responses", ...}`,
then `{"type": "done"}`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import websockets

COOKLIST_GRAPHQL = "/cooklist/graphql"
COOKLIST_TOKEN_PATH = "/commerce/recipes/customer/cooklist-auth-token"
COOKLIST_TOKEN_API_VERSION = "2025-09-18-preview"
ASSISTANT_WS_URL = (
    "wss://api.digitaldevelopment.wegmans.cloud"
    "/websockets/prod/cooklist-shopper-assistant"
)

SEND_MUTATION = """
  mutation CreateStreamingChatMessage(
    $prompt: String!
    $userId: ID!
    $sessionId: ID
    $previousMessageId: ID
    $currentUrl: String
    $shoppingCartState: ShoppingCartStateInput
    $deviceInfo: DeviceInfoInput
  ) {
    createStreamingChatMessage(
      prompt: $prompt
      userId: $userId
      sessionId: $sessionId
      previousMessageId: $previousMessageId
      currentUrl: $currentUrl
      shoppingCartState: $shoppingCartState
      deviceInfo: $deviceInfo
    ) {
      responseId
      success
      message
      userMessage { id content role session { id } }
    }
  }
"""

STREAM_SUBSCRIPTION = """
  subscription OnLLMToken($userId: String!, $sessionId: String, $responseId: String) {
    llmSubscription(userId: $userId, sessionId: $sessionId, responseId: $responseId) {
      response
    }
  }
"""

# The site sends its own onboarding consent before the first chat; mirror it
# so a first-time API caller isn't stuck behind an un-acked beta gate.
CONSENT_MUTATION = """
  mutation UpdateOnboardingStep($userId: ID!, $onboardingStep: String!, $completed: Boolean, $extraData: String) {
    updateOnboardingStep(userId: $userId, onboardingStep: $onboardingStep, completed: $completed, extraData: $extraData) {
      success
      message
    }
  }
"""


class AssistantError(RuntimeError):
    pass


class WegmansAssistant:
    """Conversation with the wegmans.com AI assistant.

    Holds `session_id` so follow-up turns continue the same conversation —
    the assistant is stateful and refers back to earlier messages.
    """

    def __init__(self, client: "Any"):
        self.client = client
        self.session_id: str | None = None
        self.last_message_id: str | None = None
        self._cooklist_token: str | None = None
        self._user_id: str | None = None

    async def _get_cooklist_token(self) -> str:
        if self._cooklist_token is None:
            r = await self.client._cloud_request(
                "GET", COOKLIST_TOKEN_PATH,
                params={"api-version": COOKLIST_TOKEN_API_VERSION},
            )
            token = (r.json() or {}).get("accessToken")
            if not token:
                raise AssistantError("no Cooklist access token returned")
            self._cooklist_token = token
        return self._cooklist_token

    async def _get_user_id(self) -> str:
        if self._user_id is None:
            customer = await self.client._get_commerce_customer()
            key = customer.get("key")
            if not key:
                raise AssistantError("commerce customer has no key")
            self._user_id = f"wgcl_{key}"
        return self._user_id

    async def _graphql(self, query: str, variables: dict, operation: str) -> dict:
        # No Authorization header here — matches the captured request; the
        # userId in variables is what identifies the account.
        r = await self.client._http.post(
            self.client.WEGMANS_CLOUD_BASE + COOKLIST_GRAPHQL,
            json={"query": query, "variables": variables, "operationName": operation},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        r.raise_for_status()
        body = r.json() or {}
        if body.get("errors"):
            raise AssistantError(f"{operation} failed: {body['errors']}")
        return body.get("data") or {}

    async def accept_terms(self) -> bool:
        """Ack the beta consent the web UI shows before the first chat."""
        data = await self._graphql(
            CONSENT_MUTATION,
            {
                "userId": await self._get_user_id(),
                "onboardingStep": "AI_AGENT_CONSENT",
                "completed": True,
                "extraData": json.dumps({"source": "wegmans_web"}),
            },
            "UpdateOnboardingStep",
        )
        return bool((data.get("updateOnboardingStep") or {}).get("success"))

    async def ask(self, prompt: str, timeout: float = 90.0) -> dict[str, Any]:
        """Send one turn and collect the streamed reply."""
        user_id = await self._get_user_id()
        token = await self._get_cooklist_token()

        # Connect before posting: tokens start streaming as soon as the
        # mutation is accepted, and a late subscriber can miss the opening.
        async with websockets.connect(
            ASSISTANT_WS_URL, subprotocols=["graphql-transport-ws"],
            open_timeout=30, close_timeout=5,
        ) as ws:
            await ws.send(json.dumps({
                "type": "connection_init",
                "payload": {"headers": {"Authorization": f"Bearer {token}"}},
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if ack.get("type") != "connection_ack":
                raise AssistantError(f"websocket refused the connection: {ack}")

            variables: dict[str, Any] = {
                "prompt": prompt,
                "userId": user_id,
                "currentUrl": "/",
                "shoppingCartState": await self._cart_state(user_id),
                "deviceInfo": {"browser": "wegmans-mcp", "platform": "python"},
            }
            if self.session_id:
                variables["sessionId"] = self.session_id
            if self.last_message_id:
                variables["previousMessageId"] = self.last_message_id

            data = await self._graphql(
                SEND_MUTATION, variables, "CreateStreamingChatMessage")
            sent = data.get("createStreamingChatMessage") or {}
            if not sent.get("success"):
                raise AssistantError(sent.get("message") or "assistant rejected the prompt")
            response_id = sent["responseId"]
            self.session_id = (
                ((sent.get("userMessage") or {}).get("session") or {}).get("id")
                or self.session_id
            )
            self.last_message_id = (sent.get("userMessage") or {}).get("id")

            await ws.send(json.dumps({
                "id": str(uuid.uuid4()),
                "type": "subscribe",
                "payload": {
                    "query": STREAM_SUBSCRIPTION,
                    "variables": {
                        "userId": user_id,
                        "sessionId": self.session_id,
                        "responseId": response_id,
                    },
                },
            }))
            return await self._collect(ws, timeout)

    async def _cart_state(self, user_id: str) -> dict[str, Any]:
        """Give the assistant the same cart context the web UI sends, so its
        answers reflect the real store and cart. Best-effort: a signed-out or
        erroring cart must not block chatting."""
        state: dict[str, Any] = {
            "userId": user_id,
            "retailerStoreLocationId": str(self.client.store_id),
            "fulfillmentType": {
                "store": "INSTORE", "curbside": "PICKUP", "delivery": "DELIVERY",
            }.get(self.client.fulfillment_type, "INSTORE"),
            "cartItemList": [],
        }
        try:
            cart = await self.client.get_grocery_cart()
            state["retailerShoppingCartIdList"] = [cart["id"]]
        except Exception:
            state["retailerShoppingCartIdList"] = []
        return state

    async def _collect(self, ws, timeout: float) -> dict[str, Any]:
        chunks: list[str] = []
        suggestions: list[str] = []
        blocks: list[dict] = []
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssistantError(
                    "assistant timed out"
                    + (f" after {''.join(chunks)[:80]!r}..." if chunks else "")
                )
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise AssistantError("assistant timed out waiting for the reply") from None

            frame = json.loads(raw)
            ftype = frame.get("type")
            if ftype in ("complete", "error"):
                if ftype == "error":
                    raise AssistantError(f"assistant stream error: {frame}")
                break
            if ftype != "next":
                continue

            payload = (((frame.get("payload") or {}).get("data") or {})
                       .get("llmSubscription") or {}).get("response")
            if not payload:
                continue
            event = json.loads(payload)
            etype = event.get("type")
            if etype == "token":
                chunks.append(event.get("data") or "")
            elif etype == "processed_block":
                if event.get("processor") == "suggested_responses":
                    suggestions = event.get("data") or []
                else:
                    blocks.append(event)
            elif etype == "done":
                break

        return {
            "reply": "".join(chunks).strip(),
            "suggested_replies": suggestions,
            "session_id": self.session_id,
            "blocks": blocks,
        }
