# OAuth2 Token Provider POC

**DO NOT COMMIT.** This directory and the related `src/` changes are throwaway
validation code. After testing, discard everything:

```bash
git checkout -- .
git clean -fd poc/
```

## What this POC validates

1. LCS fetches an OAuth2 access token at startup (before Llama Stack init)
2. When the token expires, LCS refreshes it and updates `provider_data` on the
   existing library client — **without restarting the container**

## Architecture

```
localhost                         OCP (namespace: oauth-poc)
─────────                         ─────────────────────────
LCS ──port-forward:8082──────────► Keycloak (token endpoint)
  │
  └──port-forward:8081───────────► Envoy (JWT validation)
                                      │
                                      └──► Ollama (mistral:7b)
```

## Prerequisites

- `oc` logged into an OpenShift cluster with permission to create namespaces
- Cluster can pull images from `quay.io` and `docker.io`
- Enough quota for Ollama (~4–8 Gi memory, ~10 Gi PVC for `mistral:7b`)
- Local Python env with LCS dependencies (`uv sync --group dev --group llslibdev`)

## 1. Deploy cluster resources

```bash
oc apply -f poc/k8s/namespace.yaml
oc apply -f poc/k8s/keycloak.yaml
oc apply -f poc/k8s/ollama.yaml
oc apply -f poc/k8s/envoy-gateway.yaml
```

Watch pods:

```bash
oc get pods -n oauth-poc -w
```

Expect:

- **keycloak** — ready in ~1–2 minutes
- **ollama** — ready after `mistral:7b` pull (several minutes on first boot)
- **envoy-gateway** — ready quickly; JWT validation needs Keycloak up

## 2. Port-forward (two terminals)

```bash
# Terminal A — Envoy (inference)
oc port-forward -n oauth-poc svc/envoy-gateway 8081:8080

# Terminal B — Keycloak (token endpoint)
oc port-forward -n oauth-poc svc/keycloak 8082:8080
```

## 3. Sanity-check the gateway

Fetch a token:

```bash
TOKEN=$(curl -s -X POST \
  'http://localhost:8082/realms/poc/protocol/openid-connect/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=client_credentials' \
  -d 'client_id=lcs-client' \
  -d 'client_secret=poc-secret' | jq -r .access_token)

echo "$TOKEN"
```

Call Ollama through Envoy **with** the token (should succeed):

```bash
curl -s http://localhost:8081/v1/models \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Call **without** a token (should be 401):

```bash
curl -si http://localhost:8081/v1/models | head -n 1
```

## 4. Run LCS locally

```bash
export LIGHTSPEED_STACK_CONFIG_PATH="$(pwd)/poc/lightspeed-stack.yaml"
uv run python -m uvicorn app.main:app --app-dir src --host 0.0.0.0 --port 8080
```

On startup you should see POC logs like:

```
OAuth token fetched (startup) for provider_id=vllm, expires_at=..., token=eyJ...
```

## 5. Call streaming_query

```bash
curl -N -X POST http://localhost:8080/v1/streaming_query \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Say hello in one short sentence.",
    "provider": "vllm",
    "model": "mistral:7b"
  }'
```

## 6. Observe token refresh

Keycloak access tokens expire in **120 seconds**. With
`token_expiration_leeway: 10`, LCS treats the token as expired after ~110s.

1. Make a streaming_query request immediately — should work; no refresh log
2. Wait ~2 minutes
3. Make another streaming_query request — look for:

```
Refreshing OAuth token for provider_id=vllm
OAuth token refreshed for provider_id=vllm, expires_at=..., token=eyJ...
Updated provider_data for provider_id=vllm with keys=['vllm_api_token']
```

The second request should succeed **without restarting LCS**.

## Credentials (POC defaults)

| Item | Value |
|------|-------|
| Keycloak admin | `admin` / `admin` |
| Client ID | `lcs-client` |
| Client secret | `poc-secret` |
| Realm | `poc` |
| Token lifespan | 120 seconds |

## Cleanup

```bash
oc delete namespace oauth-poc
git checkout -- .
git clean -fd poc/
```

## Known POC limitations

- Only `streaming_query` refreshes OAuth tokens (not query/responses/rlsapi)
- Raw access tokens are logged (never do this in production)
- Hard-fail on startup if token fetch fails
- Envoy skips JWT issuer validation (needed because tokens are minted via localhost port-forward)
