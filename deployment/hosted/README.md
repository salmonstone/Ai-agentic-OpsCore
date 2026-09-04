# Running AtlasOS on EC2 / EKS

If AtlasOS runs on an EC2 instance (or as a pod on an EKS node), use **IAM Role** authentication — no keys to create, rotate, or leak. This is the default and recommended method (`AWS_AUTH_METHOD=iam_role`).

## IAM Role Setup for EC2

1. **Create the policy.** In the IAM console → Policies → Create policy → JSON tab, paste the contents of [`../aws/atlasos-iam-policy.json`](../aws/atlasos-iam-policy.json). Name it e.g. `AtlasOSPolicy`.
2. **Create a role.** IAM console → Roles → Create role → Trusted entity: **AWS service → EC2**. Attach the `AtlasOSPolicy` you just created.
3. **Attach the role to your instance.** EC2 console → select your instance → **Actions → Security → Modify IAM role** → choose the role → Update.
4. **Grant cluster access.** The role also needs to be recognized by your EKS cluster's RBAC — either add it as an EKS access entry (`aws eks create-access-entry --principal-arn <role-arn> ...`) or map it in the `aws-auth` ConfigMap, depending on your cluster's authentication mode.
5. **Run setup.** `agent setup` → AWS/Kubernetes section → choose "On an EC2 instance or EKS node" (option 1). No keys needed — the wizard verifies the attached role automatically.

No keys are ever stored. If you rotate or replace the role, nothing in AtlasOS's config needs to change.

## Running as an EKS pod

Same idea via IRSA (IAM Roles for Service Accounts): create the role with an EKS-cluster trust policy instead of an EC2 one, attach the same `AtlasOSPolicy`, and annotate the pod's ServiceAccount with the role ARN. `AWS_AUTH_METHOD=iam_role` still applies — boto3 and the AWS CLI resolve IRSA credentials the same way as instance-profile credentials.
