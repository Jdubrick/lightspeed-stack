OAuth2 Token Provider for Inference Providers — Design Document
Status: Draft
Scope: Library mode only (initial implementation: vLLM behind OAuth2 gateway, e.g. Apigee, AWS API Gateway, Azure API Management, NGINX Plus)
Date: 2026-07-03
Design Decisions (resolved via review)
#
Decision
Rationale
1
Skip model validation on token refreshes; only validate at initial startup
Avoids full Llama Stack re-init every 30-60 min
2
Dynamic model discovery at startup (fetch token before Llama Stack init)
Operator doesn't need to hardcode allowed_models; frontend /v1/models endpoint auto-populates
3
Update provider_data directly on existing client instance (no client re-creation on refresh)
Lighter than Azure's full re-init approach; provider_data is a public attribute on the library client
4
asyncio.Lock with double-checked locking for concurrent refresh
Prevents thundering herd; standard async pattern
5
Skip A2A endpoint (matching existing Azure gap)
Not currently used with OAuth providers; document as known gap
6
Env var name for OAuth token is auto-generated internally (no config field)
Operator never sets this env var — it's a bridge between LCS and Llama Stack's ${env.*} resolution
7
Startup failure: hard fail if OAuth provider is the only provider; graceful degradation if others exist
Balances reliability with availability



Table of Contents
Problem Statement
Background: How Credentials Work Today
Comparison: Azure Entra ID vs. Proposed OAuth Provider
Proposed Design
Startup Sequence
Per-Request Token Refresh
What Would Need to Be Done
Feasibility Assessment
Risks and Mitigations
Out of Scope


Problem Statement
Today, LLM inference provider credentials in Lightspeed Core Stack (LCS) are typically configured as static environment variables referenced in Llama Stack run configuration:

# Synthesized run.yaml (example)
providers:
  inference:
    - provider_id: vllm
      provider_type: remote::vllm
      config:
        api_token: ${env.VLLM_API_TOKEN}

In Kubernetes deployments, VLLM_API_TOKEN is injected from a Secret at container startup. When the token is short-lived (e.g., an OAuth2 access token issued by an API gateway such as Apigee, AWS API Gateway, Azure API Management, or NGINX Plus), operators must:

Update the Kubernetes Secret with a fresh token
Restart the LCS container so the new env var is picked up

This is cumbersome for tokens that expire every 30–60 minutes.

There is currently no generic mechanism in LCS for dynamically fetching and refreshing LLM provider tokens. The only exception is Azure Entra ID, which implements OAuth2 client-credentials flow for Azure OpenAI specifically.


Background: How Credentials Work Today
LCS uses a two-layer credential model:

Layer
Responsibility
LCS (FastAPI)
Connects to Llama Stack, handles user auth, Azure Entra ID token refresh, MCP auth forwarding
Llama Stack
Calls external LLM providers (OpenAI, vLLM, Azure, etc.) using credentials from run.yaml

Credential resolution timeline
LCS config load — lightspeed-stack.yaml is parsed; ${env.*} references are resolved via Llama Stack's replace_env_vars.
Llama Stack client init — AsyncLlamaStackClientHolder().load() creates either a remote HTTP client or an in-process library client.
Library mode init — AsyncLlamaStackAsLibraryClient.initialize() reads run.yaml, resolves ${env.*} again, and may call provider endpoints (e.g., /v1/models) if model_validation is enabled.
Inference requests — LCS calls Llama Stack; Llama Stack forwards provider credentials to the external API.
Current startup order (library mode)
1. configuration.load_configuration()
2. AsyncLlamaStackClientHolder().load()
   └→ AsyncLlamaStackAsLibraryClient(config_path)
   └→ client.initialize()          ← model validation may call vLLM here
3. AzureEntraIDManager().set_config()   ← Azure setup happens AFTER init

Azure works around the startup problem by setting model_validation: false during config synthesis, so Llama Stack never calls Azure at init. Tokens are fetched lazily on the first inference request.


Comparison: Azure Entra ID vs. Proposed OAuth Provider
Aspect
Azure Entra ID (existing)
Proposed OAuth Provider
Config location
Top-level azure_entra_id block in lightspeed-stack.yaml
Nested oauth field on each UnifiedInferenceProvider
Config fields
tenant_id, client_id, client_secret, scope
token_url, client_id, client_secret, scope (optional), token_expiration_leeway
Token manager
AzureEntraIDManager singleton in src/authorization/azure_token_manager.py
Generic OAuthTokenManager in src/authorization/oauth_token_manager.py
Token fetch library
azure-identity (ClientSecretCredential)
Plain httpx POST (RFC 6749 client_credentials grant)
Provider scope
Hardcoded to remote::azure
Provider-agnostic; initially validated with vLLM
Deployment mode
Works in both service and library mode
Library mode only (requires startup ordering control)
Startup behavior
Sets model_validation: false; skips provider calls at init
Fetches token before Llama Stack init; enables /v1/models discovery
Initial token timing
Lazy — first inference request
Eager — before AsyncLlamaStackClientHolder().load()
Per-request refresh
Check expiry in 4 endpoints before inference
Same pattern for OAuth-configured providers
Client update mechanism
update_azure_token() → recreates AsyncLlamaStackAsLibraryClient with updated provider_data
update_oauth_token() → mutates provider_data dict on existing client (no re-init)
Re-init on refresh
Full library client re-creation on every token refresh
No re-init; direct provider_data update; next request picks up fresh token via context var
Long-lived credentials
client_id / client_secret in K8s Secret (rarely rotated)
Same — only access tokens are short-lived

Key code references (Azure precedent)
Config model — src/models/config.py:.

class AzureEntraIdConfiguration(ConfigurationBase):
    tenant_id: SecretStr
    client_id: SecretStr
    client_secret: SecretStr
    scope: str = "https://cognitiveservices.azure.com/.default"

Startup enrichment — src/llama_stack_configuration.py:

def enrich_azure_entra_id_inference(ls_config, azure_entra_id):
    # Sets model_validation=false on remote::azure providers
    provider_config["model_validation"] = False

Per-request refresh — src/app/endpoints/query.py:

if (
    responses_params.model.startswith("azure")
    and AzureEntraIDManager().is_entra_id_configured
    and AzureEntraIDManager().is_token_expired
    and AzureEntraIDManager().refresh_token()
):
    client = await AsyncLlamaStackClientHolder().update_azure_token()

Client update (library mode) — src/client.py — Azure recreates the entire client:

client = AsyncLlamaStackAsLibraryClient(
    self._config_path, provider_data=current_provider_data
)
await client.initialize()
self._lsc = client

The proposed OAuth approach is lighter: mutate provider_data on the existing client instead (see Design Decision #3).


Proposed Design
Configuration Model
Add a new OAuthConfiguration Pydantic model and an optional oauth field on UnifiedInferenceProvider:

# lightspeed-stack.yaml — Apigee example
llama_stack:
  use_as_library_client: true

inference:
  providers:
    - type: vllm
      extra:
        base_url: https://apigee.example.com/vllm
      oauth:
        token_url: https://apigee.example.com/oauth/token
        client_id: ${env.OAUTH_CLIENT_ID}
        client_secret: ${env.OAUTH_CLIENT_SECRET}
        scope: "inference"
        token_expiration_leeway: 30

Note: there is no api_key_env field. When oauth is configured, LCS auto-generates an internal env var name (e.g., _LCS_OAUTH_VLLM_TOKEN) and emits it as ${env._LCS_OAUTH_VLLM_TOKEN} in the synthesized run.yaml. The operator never needs to know about it — it's a bridge between LCS's in-memory token and Llama Stack's ${env.*} resolution.

The same configuration works with any OAuth2-compliant gateway. Only base_url and token_url change:

# AWS API Gateway + Cognito
- type: vllm
  extra:
    base_url: https://abc123.execute-api.us-east-1.amazonaws.com/prod/vllm
  oauth:
    token_url: https://my-domain.auth.us-east-1.amazoncognito.com/oauth2/token
    client_id: ${env.OAUTH_CLIENT_ID}
    client_secret: ${env.OAUTH_CLIENT_SECRET}
    scope: "vllm/inference"

# Azure API Management + Entra ID
- type: vllm
  extra:
    base_url: https://my-apim.azure-api.net/vllm
  oauth:
    token_url: https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token
    client_id: ${env.OAUTH_CLIENT_ID}
    client_secret: ${env.OAUTH_CLIENT_SECRET}
    scope: "api://my-apim-app/.default"

# NGINX Plus with Keycloak
- type: vllm
  extra:
    base_url: https://nginx.internal.example.com/vllm
  oauth:
    token_url: https://keycloak.example.com/realms/ml/protocol/openid-connect/token
    client_id: ${env.OAUTH_CLIENT_ID}
    client_secret: ${env.OAUTH_CLIENT_SECRET}
    scope: "inference"

Pydantic model (proposed):

class OAuthConfiguration(ConfigurationBase):
    """OAuth2 client credentials configuration for inference providers."""

    token_url: AnyHttpUrl
    client_id: SecretStr
    client_secret: SecretStr
    scope: Optional[str] = None
    token_expiration_leeway: PositiveInt = 30

Validation rules:

Rule
Rationale
oauth and api_key_env are mutually exclusive on the same provider
OAuth replaces static key injection
oauth requires llama_stack.use_as_library_client: true
Service mode cannot control startup ordering


When oauth is configured, the config synthesizer auto-generates an internal env var name based on the provider_id (e.g., _LCS_OAUTH_VLLM_TOKEN for provider_id vllm) and emits it as ${env._LCS_OAUTH_VLLM_TOKEN} in the synthesized run.yaml. LCS sets this env var programmatically at startup with the fetched token. The operator never interacts with this env var — it is an internal bridge between LCS and Llama Stack's ${env.*} resolution.
OAuthTokenManager
A generic token manager mirroring AzureEntraIDManager:

class OAuthTokenManager:
    """Manages OAuth2 client-credentials access tokens for inference providers."""

    _access_token: SecretStr
    _expires_on: int
    _oauth_config: OAuthConfiguration
    _provider_id: str
    _provider_data_key: str  # e.g., "vllm_api_token"
    _lock: asyncio.Lock  # prevent concurrent refresh races

    async def fetch_initial_token(self) -> str: ...

    @property
    def is_token_expired(self) -> bool: ...

    async def refresh_token(self) -> bool:
        """Refresh with double-checked locking.

        Acquires the lock, re-checks expiry (another request may have
        refreshed while waiting), and only fetches if still expired.
        """
        ...

    def build_provider_data(self) -> dict[str, str]:
        """Return e.g. {"vllm_api_token": "<token>"}."""
        ...

Token fetch (RFC 6749 client_credentials grant):

POST {token_url}
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={client_id}
&client_secret={client_secret}
&scope={scope}          # if configured

Response:

{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600
}

The manager caches access_token and computes _expires_on = now + expires_in - leeway.

Location: src/authorization/oauth_token_manager.py

A registry (e.g., OAuthTokenManagerRegistry) holds one manager per OAuth-configured provider, initialized during config load. The registry is keyed by synthesized provider_id (e.g., vllm, vllm-rhaiis, vllm-rhel-ai). All vLLM variants (vllm, vllm_rhaiis, vllm_rhel_ai) map to remote::vllm and use the same provider_data_api_key_field: "vllm_api_token", but each can have its own OAuth endpoint and credentials.
Client Update Mechanism
Unlike the Azure implementation (which recreates the entire AsyncLlamaStackAsLibraryClient on every refresh), the OAuth provider updates provider_data directly on the existing client instance. This avoids a full Llama Stack re-initialization on every token refresh.

The provider_data attribute on AsyncLlamaStackAsLibraryClient is a plain public dict. The library client injects it as an X-LlamaStack-Provider-Data header on every request via a contextvars.ContextVar. The vLLM provider's OpenAIMixin._get_api_key_from_config_or_provider_data() checks this context var on every inference call, so updating the dict is sufficient — no client re-creation needed.

def update_oauth_provider_data(self, provider_id: str) -> None:
    """Update provider_data on the held library client with fresh OAuth token."""
    manager = OAuthTokenManagerRegistry.get(provider_id)
    updates = manager.build_provider_data()  # e.g., {"vllm_api_token": "<token>"}
    if not updates:
        return
    client = cast(AsyncLlamaStackAsLibraryClient, self._lsc)
    if client.provider_data is None:
        client.provider_data = {}
    client.provider_data.update(updates)

The provider_data key name must match the provider's provider_data_api_key_field. Known mappings:

Provider type
provider_data_api_key_field
vLLM
vllm_api_token
OpenAI
openai_api_key
Azure
azure_api_key
WatsonX
watsonx_api_key
Vertex AI
gemini_api_key



Startup Sequence
Why library mode only
In library mode, LCS and Llama Stack run in the same process. LCS controls the order of operations and can fetch an OAuth token before calling AsyncLlamaStackAsLibraryClient.initialize().

In service mode, Llama Stack is a separate container that starts independently. LCS cannot inject a token before Llama Stack initializes. Service mode would require either:

An init container / sidecar to fetch tokens before Llama Stack starts (outside LCS)
Native OAuth support inside Llama Stack itself (outside LCS)

Therefore, OAuth provider config is rejected at validation time if use_as_library_client is not true.
Proposed startup sequence (conditional)
This block runs only when at least one inference provider has oauth configured:

1. configuration.load_configuration()
2. NEW: For each OAuth-configured provider:
     a. OAuthTokenManager.fetch_initial_token()
     b. os.environ[auto_generated_env_var_name] = token
3. AsyncLlamaStackClientHolder().load()
   └→ AsyncLlamaStackAsLibraryClient(config_path)
   └→ client.initialize()
      └→ Llama Stack resolves ${env.VLLM_API_TOKEN} → valid token
      └→ Calls /v1/models (model_validation: true) → discovers models dynamically
4. AzureEntraIDManager setup (unchanged, if configured)
5. MCP registration, database init, etc.

Key difference from Azure: OAuth enables dynamic model discovery because the token is available before Llama Stack initializes. No need for model_validation: false or hardcoded allowed_models.

Insertion point in code: src/app/main.py, between lines 78 and 83:

configuration.load_configuration(os.environ["LIGHTSPEED_STACK_CONFIG_PATH"])

# NEW: conditional OAuth token prefetch
await prefetch_oauth_tokens(configuration.configuration)

llama_stack_config = configuration.configuration.llama_stack
await AsyncLlamaStackClientHolder().load(llama_stack_config)

If no providers have oauth configured, prefetch_oauth_tokens() is a no-op and startup is unchanged.
Startup failure behavior
If the initial token fetch fails for an OAuth-configured provider after all retries:

If it is the only inference provider: hard fail — LCS refuses to start with a clear error message. Starting without any working inference provider would just defer failure to the first user request.
If other inference providers exist: graceful degradation — log an error, remove the failed provider from the synthesized config, and continue startup with the remaining providers. The /v1/models response will only include models from healthy providers.


Per-Request Token Refresh
After startup, the cached token lives in memory. Before each inference call, endpoints check expiry and refresh if needed — identical to the Azure pattern:

Request arrives
  → Is the model served by an OAuth-configured provider?
    → No:  proceed normally
    → Yes: Is token expired (or within leeway)?
      → No:  proceed with cached token
      → Yes: fetch new token from OAuth provider
             → update client via provider_data
             → proceed

Endpoints to update (same 4 as Azure):

File
Current Azure check
src/app/endpoints/query.py
Lines 221–228
src/app/endpoints/streaming_query.py
Lines 277–284
src/app/endpoints/responses.py
Lines 410–417
src/app/endpoints/rlsapi_v1.py
Lines 270–277


Model-to-provider resolution: Model IDs follow the pattern provider_id/model_name (e.g., vllm/meta-llama/Llama-3-8b). The existing extract_provider_and_model_from_model_id() function splits on the first / to extract the provider prefix. The endpoint uses this prefix to look up the OAuthTokenManager from the registry.

Proposed check (pseudocode):

provider_id, _ = extract_provider_and_model_from_model_id(responses_params.model)
oauth_manager = OAuthTokenManagerRegistry.get(provider_id)
if oauth_manager and oauth_manager.is_token_expired:
    if await oauth_manager.refresh_token():
        AsyncLlamaStackClientHolder().update_oauth_provider_data(provider_id)

The expiry check is cheap (timestamp comparison). The HTTP call to the OAuth token endpoint only happens when the token is expired or within the leeway window. The refresh_token() method uses double-checked locking: it acquires an asyncio.Lock, re-checks expiry (another request may have refreshed while waiting), and only performs the HTTP call if the token is still expired.

Note: update_oauth_provider_data() mutates provider_data on the existing client — it does not recreate the library client. This is a key difference from Azure's update_azure_token().


What Would Need to Be Done
New files
File
Purpose
src/authorization/oauth_token_manager.py
OAuthTokenManager, OAuthTokenManagerRegistry, token fetch/refresh logic
tests/unit/authorization/test_oauth_token_manager.py
Unit tests for token manager
tests/unit/test_oauth_startup.py
Tests for conditional startup prefetch

Modified files
File
Change
src/models/config.py
Add OAuthConfiguration model; add oauth field to UnifiedInferenceProvider; add validation (library mode only, mutual exclusion with api_key_env)
src/app/main.py
Add conditional prefetch_oauth_tokens() call before client init
src/client.py
Add update_oauth_provider_data(provider_id) method — mutates provider_data on existing client (no re-init, unlike Azure's approach)
src/llama_stack_configuration.py
Update synthesizer to auto-generate env var name and emit ${env._LCS_OAUTH_<PROVIDER_ID>_TOKEN} when OAuth is configured
src/app/endpoints/query.py
Add OAuth token refresh check
src/app/endpoints/streaming_query.py
Add OAuth token refresh check
src/app/endpoints/responses.py
Add OAuth token refresh check
src/app/endpoints/rlsapi_v1.py
Add OAuth token refresh check

Not changed (by design)
File
Reason
src/authorization/azure_token_manager.py
Azure stays as-is; no refactor
Service mode client paths
OAuth is library-mode only

Example deployment (Kubernetes)
Long-lived credentials (rarely rotated):

# Kubernetes Secret
apiVersion: v1
kind: Secret
metadata:
  name: lcs-oauth-credentials
stringData:
  OAUTH_CLIENT_ID: "my-lcs-client"
  OAUTH_CLIENT_SECRET: "long-lived-secret"

LCS deployment env:

env:
  - name: OAUTH_CLIENT_ID
    valueFrom:
      secretKeyRef:
        name: lcs-oauth-credentials
        key: OAUTH_CLIENT_ID
  - name: OAUTH_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: lcs-oauth-credentials
        key: OAUTH_CLIENT_SECRET
  # VLLM_API_TOKEN is NOT set here — LCS manages it in memory

The short-lived access token never touches a K8s Secret. LCS fetches it from the OAuth2 token endpoint (Apigee, AWS Cognito, Azure Entra ID, Keycloak, etc.) at startup and refreshes it in memory for the lifetime of the process.


Feasibility Assessment
Dimension
Assessment
Overall effort
Medium — most patterns already exist in the Azure Entra ID implementation
New dependencies
None — httpx is already available via llama-stack-client
Complexity
Low-to-medium — token manager is ~100 lines; startup change is ~20 lines; endpoint changes are copy-paste from Azure pattern
Testing
Straightforward — mock OAuth token endpoint with pytest-httpx or respx; test startup ordering, expiry, refresh, and failure paths
Risk to existing deployments
Very low — all changes are conditional on oauth being configured; no impact on static-key or Azure deployments

Effort breakdown (estimate)
Task
Estimate
Config model + validation
0.5 day
OAuthTokenManager + registry
1 day
Startup prefetch integration
0.5 day
Client update method
0.5 day
Endpoint refresh checks (4 files)
0.5 day
Config synthesizer update
0.5 day
Unit + integration tests
1 day
Documentation + example config
0.5 day
Total
~5 days



Risks and Mitigations
Risk
Impact
Mitigation
Token fetch fails at startup
Provider unavailable at init
Retry with exponential backoff. If only provider: hard fail. If others exist: remove failed provider, continue with rest.
Token refresh fails mid-operation
Inference calls fail with 401 from the API gateway
Log warning; retry refresh once; return clear HTTP 502/503 error to caller with actionable message
Race condition on concurrent refresh
Multiple requests see expired token simultaneously, all hit OAuth endpoint
asyncio.Lock per provider with double-checked locking; first request refreshes, others re-check and skip
Endpoint duplication
Refresh logic copy-pasted across 4 endpoints (same as Azure today)
Accept for v1 (matches Azure precedent); future work could extract to a shared dependency or middleware
Library mode constraint surprises users
Operator configures OAuth in service mode, gets validation error
Clear validation error at config load: "oauth provider config requires llama_stack.use_as_library_client: true"
OAuth provider downtime
All inference to that provider fails
Same as any external dependency; degraded mode / health probe could surface provider unavailability
Token scope misconfiguration
Token issued but rejected by the API gateway
Document required scope; log token fetch response on failure; include scope in config validation docs
Direct provider_data mutation
Mutating a public attribute on a third-party class; could break on library upgrade
Attribute is part of the library client's public interface; add an integration test verifying the attribute exists and is used



Out of Scope
The following are explicitly not part of this design:

Service mode support — requires infrastructure outside LCS (init containers, Llama Stack native OAuth)
Refactoring Azure Entra ID to use the new generic mechanism — Azure works; leave it alone
Other OAuth grant types — only client_credentials (machine-to-machine); no authorization code, PKCE, or on-behalf-of flows
File-watching / K8s Secret rotation — tokens are managed in memory, not by watching external files
Non-vLLM provider validation — config model is general, but initial testing is scoped to vLLM behind an OAuth2 gateway
A2A endpoint OAuth refresh — Azure refresh is also missing from a2a.py; same gap applies here (future work)


Summary
Adding a generic OAuth2 client-credentials token provider to LCS is feasible and medium effort. The design follows the proven Azure Entra ID pattern but generalizes it for any inference provider behind any OAuth2-compliant gateway (Apigee, AWS API Gateway, Azure API Management, NGINX Plus, Kong, Envoy/Istio, etc.) using any OAuth2 identity provider (Keycloak, Okta, AWS Cognito, Azure Entra ID, Auth0, etc.). The key innovation over Azure is conditional startup token prefetch, which enables dynamic model discovery from /v1/models without requiring hardcoded allowed_models.

The feature is library mode only because LCS must control startup ordering to fetch the token before Llama Stack initializes. Long-lived client_id/client_secret credentials remain in K8s Secrets (rarely rotated); only the short-lived access token is managed in memory by LCS.

After some investigation, OGX (Llama Stack) does not currently have any support for vLLM to use anything other than a static token. This unfortunately means that having dynamic tokens in server mode is, as far as I understand, not possible. This is due to the fact that Lightspeed Core needs Llama Start to be running before itself in server mode, so there must be a static token for vLLM to be registered in that case. In order to add dynamic tokens for server mode, an RFE would need to be made to OGX (Llama Stack).
