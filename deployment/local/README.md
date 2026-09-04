# Running AtlasOS Locally

AtlasOS supports three ways to authenticate with AWS. `agent setup` walks you through picking one; `agent aws switch-auth` re-runs just that step later, and `agent aws auth-status` shows what's active.

## AWS Authentication

| Method | `AWS_AUTH_METHOD` | When to use |
|---|---|---|
| **IAM Role** | `iam_role` (default) | Only works when AtlasOS actually runs on EC2/EKS — see [`../hosted/README.md`](../hosted/README.md). Not usable from a laptop. |
| **Access Keys** | `access_key` | Local development, quick start, no AWS SSO set up. Static long-lived credentials — keep `.env` out of Git. |
| **SSO Profile** | `sso_profile` | Local development on a team that already uses AWS SSO / IAM Identity Center, or any named `~/.aws` profile (assumed role, etc.). Short-lived, auto-refreshing — no static keys stored. |

### Access Keys

```bash
agent setup
# AWS/Kubernetes → [2] On my local laptop → [1] AWS Access Key + Secret Key
```
Get a key from IAM Console → Users → your user → Security credentials → Create access key. Written to `.env` as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — never commit that file.

### SSO Profile

```bash
agent setup
# AWS/Kubernetes → [2] On my local laptop → [2] AWS SSO Named Profile
```
Picks from your existing `~/.aws` profiles, or offers to run `aws configure sso` to create one. Only `AWS_PROFILE` (a name, not a secret) is written to `.env`.

## Switching methods later

```bash
agent aws switch-auth     # re-run just the AWS auth step
agent aws auth-status     # check what's currently configured and whether it works
```

## Kubernetes

Whichever AWS method you pick, the wizard also runs `aws eks update-kubeconfig` for you if you give it an EKS cluster name — no separate `agent k8s add-cluster` step needed. Use `agent k8s contexts` / `agent k8s switch` afterward to manage multiple clusters.
