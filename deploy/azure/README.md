# Azure AKS deployment — pipecat voice bot (functional-test scale)

Stands up the pipecat FreeSWITCH voice bot on **AKS**, sized for a **small functional test**
(~1–100 concurrent), with **mocked AI** so it costs ~$0 in Deepgram/Cartesia/Anthropic spend.
Everything is **parameterized** so you scale the same templates up later.

> ⚠️ These templates were authored offline and have **not** been run against a live Azure
> subscription. Treat them as a reviewed starting point: read them, then run
> `scripts/deploy.ps1` with your own `az login`. The one piece needing a live voice smoke test
> is the mocked STT/TTS path (see *Mocked AI* below).

## Decisions baked in
- **IaC:** Bicep (`bicep/`), orchestrated by `scripts/deploy.ps1`.
- **AI:** mocked in-cluster (no GPUs, no vendor keys) — for infra/media/scale testing only.
- **FreeSWITCH:** runs **inside AKS on `hostNetwork`**, on a dedicated node pool whose nodes get
  **public IPs**; NSG opens SIP + the RTP UDP range.
- **Scale:** small; bot pool autoscales 1→3, FS pool fixed 1 node.

## What gets created in Azure
| Layer | Resource | Notes |
|---|---|---|
| Group | Resource group | one, easy teardown |
| Network | VNet + subnet + **NSG** | NSG opens `5060` (SIP) and `16384–32768/udp` (RTP) inbound |
| Registry | **ACR** | holds the bot image (built + pushed by the deploy script) |
| Cluster | **AKS** (Azure CNI, BYO subnet) | 3 node pools ↓ |
| Pool `system` | 1× small VM | k8s system pods |
| Pool `bots` | autoscale 1–3 | the pipecat bot Deployment |
| Pool `freeswitch` | 1× VM, **node public IP**, tainted | FreeSWITCH `hostNetwork` pod |

## k8s workloads (`k8s/`)
- `freeswitch-deployment.yaml` — `hostNetwork: true`, `dnsPolicy: ClusterFirstWithHostNet` (so it
  can still resolve `bot-svc`), pinned to the `freeswitch` pool via nodeSelector+toleration. Forks
  audio to `ws://bot-svc:7860/audio?uuid=…`.
- `bot-deployment.yaml` + `bot-service.yaml` — the bot (ClusterIP `bot-svc:7860`), `MOCK_AI=true`,
  no vendor keys. `preStop` drain + long grace period so in-flight calls finish on scale-down.
- `bot-hpa.yaml` — HPA (CPU placeholder; swap for KEDA active-calls later).
- `mock-llm-deployment.yaml` — optional standalone OpenAI-compatible mock (if you prefer mocking the
  LLM over the network instead of in-process).
- `secrets.example.yaml` — FS ESL password (copy to `secrets.yaml`, don't commit).

## Prerequisites
- `az` CLI, `kubectl`, `docker`, `bicep` (`az bicep install`), PowerShell.
- `az login` and `az account set --subscription <id>`.
- Quotas: enough vCPU in the region for 3 small node pools.

## Deploy
```powershell
cd deploy/azure/scripts
./deploy.ps1 -ResourceGroup pcbot-test -Location westeurope -Prefix pcbot
```
The script: creates the RG → `az deployment group create` (Bicep: network/ACR/AKS) → `az acr build`
the bot image → `az aks get-credentials` → `kubectl apply -f ../k8s`.

## Test
- Point a SIP client / SIPp at the **FreeSWITCH node's public IP** (printed at the end of deploy).
- MicroSIP: 1–2 calls to confirm the path. SIPp from an in-region VM for concurrency.
- `kubectl logs`, `kubectl get hpa`, `kubectl top pods` to watch scaling.

## Teardown (do this — compute bills by the hour)
```powershell
./teardown.ps1 -ResourceGroup pcbot-test
```
Deletes the whole resource group.

## Mocked AI — the one thing to finish
`MOCK_AI=true` tells the bot to skip Deepgram/Cartesia/Anthropic. Two ways to realize it:
1. **In-process bot mocks** (recommended for a true capacity test): `MockSTTService` emits a canned
   transcript on speech-stop, `MockLLMService` returns a canned reply, `MockTTSService` emits a
   fixed PCM buffer. Keeps real Silero VAD + serializer load (the actual per-pod CPU) with zero
   network. **These need implementing in the bot and a live voice smoke test** — I can add them next.
2. **Network mock LLM** (`mock-llm-deployment.yaml`): point `OpenAILLMService(base_url=...)` at it.
   Simplest for the LLM, but STT/TTS still need option 1 for a $0 run.

Until the in-process mocks land, run the deploy with real keys at tiny scale to validate wiring,
then switch to mocks for the capacity numbers.
