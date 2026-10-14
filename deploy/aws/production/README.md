# Despliegue de producción en AWS

Terraform define una VPC privada multi-AZ, EKS, PostgreSQL privado con pgvector y failover Multi-AZ, Valkey TLS con tres nodos, EFS cifrado con AWS Backup, dos buckets privados con S3 Object Lock y repositorios ECR inmutables. Kubernetes ejecuta la API, workers de eventos, migraciones, copias de PostgreSQL y archivado de auditoría.

La cuenta cloud, el dominio DNS, Keycloak/Vault/Langfuse, las credenciales de proveedores y los rangos de red aprobados pertenecen a la organización que opere el sistema. El repositorio contiene automatización y comprobaciones; no afirma que existan esas cuentas o recursos antes de que sus responsables los aprovisionen.

## Requisitos

- Terraform 1.10 o posterior, AWS CLI con acceso SSO/rol asumido autorizado, Docker, Helm, kubectl y Python 3.
- CIDR real de VPN/runner para restringir el endpoint EKS y un dominio en una hosted zone pública existente de Route 53.
- Realm `a2a` de Keycloak, Vault con autenticación Kubernetes y credenciales de proyecto de Langfuse.
- Checkout privado del repositorio para operar el estado y la configuración de Terraform.

## 1. Crear el bucket remoto para el estado Terraform

El stack inicial usa estado local solo durante este paso. Mantén ese estado privado y consérvalo según la política de infraestructura de tu organización.

```sh
terraform -chdir=deploy/aws/state-backend init
terraform -chdir=deploy/aws/state-backend apply -var='region=us-east-1'
terraform -chdir=deploy/aws/state-backend output
```

Copia `backend.hcl.example` como `backend.hcl` (archivo local ignorado) e indica el bucket y ARN KMS devueltos. Copia `terraform.tfvars.example` como `terraform.tfvars` e indica la región, el hostname público y su hosted zone de Route 53, los CIDR reales de confianza y nombres globalmente únicos para los buckets de auditoría y backups.

## 2. Crear la infraestructura de producción

Proporciona `TF_VAR_database_admin_password` y `TF_VAR_valkey_auth_token` desde el almacén de secretos protegido. No los escribas en archivos ni en el historial del shell. El estado cifrado también contiene esos valores; limita su acceso a administradores de infraestructura.

```sh
terraform -chdir=deploy/aws/production init -backend-config=backend.hcl
terraform -chdir=deploy/aws/production validate
terraform -chdir=deploy/aws/production plan -out=production.tfplan
terraform -chdir=deploy/aws/production apply production.tfplan
```

Antes de aplicar, revisa cuotas, disponibilidad regional, capacidad de instancias, costes de NAT, backups, retención y protección frente a borrado. Obtén los datos del clúster:

```sh
export AWS_REGION="$(terraform -chdir=deploy/aws/production output -raw region)"
export CLUSTER_NAME="$(terraform -chdir=deploy/aws/production output -raw cluster_name)"
```

## 3. Publicar imágenes e instalar los componentes del clúster

Terraform crea repositorios ECR privados, con tags inmutables y análisis de imágenes. Desde un checkout autorizado para publicar en ECR:

```sh
deploy/aws/production/publish_images.sh
```

El script construye y publica las imágenes de la API y de operaciones con el tag `release-<git-sha>`, e imprime los dos valores para `production.env`. Puedes definir `IMAGE_TAG` para elegir otro tag de release.

Instala AWS Load Balancer Controller cuando EKS esté listo. Terraform crea su política IAM fijada a la versión upstream `v3.6.0`; el rol solo puede asumirlo su ServiceAccount de Kubernetes. El script instala el controlador y comprueba que esté disponible antes de crear el ALB:

```sh
deploy/k8s/cluster-addons.sh
kubectl -n kube-system get deployment aws-load-balancer-controller
```

Terraform solicita un certificado ACM para `public_domain` y crea sus registros de validación DNS en la hosted zone existente. Tras aplicar la plataforma, crea automáticamente el alias A al ALB:

```sh
deploy/aws/production/publish_dns.sh
```

El ingreso público usa AWS Load Balancer Controller directamente; el listener HTTP redirige a HTTPS y las peticiones llegan por HTTPS al ALB. No hace falta ejecutar un proxy de ingress adicional ni un issuer ACME en Kubernetes.

## 4. Configurar Vault, PostgreSQL y Valkey

Define `VAULT_ADDR`, `VAULT_PROVISIONER_TOKEN` y, si corresponde, `VAULT_NAMESPACE` en el shell de aprovisionamiento protegido. El token necesita permisos para habilitar/configurar KV v2 y Kubernetes auth, escribir las políticas y roles indicados, y guardar los secretos de base de datos y Valkey.

Configura Kubernetes auth con un JWT de revisión y el certificado CA del clúster:

```sh
export KUBERNETES_API_URL="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME" --query 'cluster.endpoint' --output text)"
export KUBERNETES_TOKEN_REVIEWER_JWT="<token temporal de una service account revisora del clúster>"
export KUBERNETES_CA_FILE="<ruta privada al certificado CA PEM de EKS>"
python deploy/vault/configure_kubernetes_auth.py
```

El script crea políticas y roles separados para la API, la migración, los backups y el archivador de auditoría. La aplicación no puede leer la credencial de migración ni los secretos de los clientes-agente.

Escribe en Vault las URLs administrativas generadas desde Terraform y Valkey; después crea el usuario PostgreSQL de mínimo privilegio para la aplicación:

```sh
python deploy/aws/production/store_runtime_secrets.py --terraform-dir deploy/aws/production
deploy/aws/production/fetch_rds_ca.sh .secrets/rds-global-bundle.pem
export LOCAL_RDS_CA_BUNDLE="$PWD/.secrets/rds-global-bundle.pem"
python deploy/aws/production/bootstrap_database.py --terraform-dir deploy/aws/production
```

Las URLs exigen TLS y verifican el certificado del servidor con el bundle global CA de AWS RDS. Las imágenes de aplicación y operaciones incluyen ese bundle; el bootstrap local utiliza el archivo indicado por `LOCAL_RDS_CA_BUNDLE`.

## 5. Guardar credenciales externas en Vault

Guarda los valores en KV v2. Añade solo las integraciones habilitadas en esta instalación.

| Ruta Vault | Campos |
| --- | --- |
| `a2a/production/providers/model` | `api_key` |
| `a2a/production/observability/langfuse` | `public_key`, `secret_key` |
| `a2a/production/integrations/jira` | `api_token` |
| `a2a/production/integrations/github` | `token` |
| `a2a/production/integrations/gmail` | `client_id`, `client_secret`, `refresh_token` |
| `a2a/production/integrations/mqtt` | `broker_url`, `username`, `password` (perfil IoT opcional) |

La política OPA está en `deploy/policy/agentic.rego`; el renderer la incorpora al manifiesto desplegado en `deploy/k8s/opa.yaml`. La aplicación deniega la operación si no puede consultar su servicio interno de decisiones. Langfuse recibe las trazas OTLP HTTP con credenciales obtenidas de Vault, sin persistirlas en SQL ni en el ConfigMap.

## 6. Renderizar, aplicar y verificar Kubernetes

Copia el ejemplo a un archivo privado e ignorado por Git:

```sh
cp deploy/k8s/production.env.example deploy/k8s/production.env
```

Completa URL pública (su hostname debe coincidir con `public_domain`), issuer de Keycloak, Vault, endpoint/nombre del modelo, Langfuse, hosts permitidos para conectores e imágenes. El renderer combina esos valores con salidas de Terraform, valida HTTPS y todos los documentos YAML y falla si encuentra valores de ejemplo, outputs ausentes o marcadores pendientes.

```sh
python deploy/k8s/render.py --env-file deploy/k8s/production.env \
  --terraform-dir deploy/aws/production --output-dir dist/k8s
kubectl apply -f dist/k8s/namespace.yaml
kubectl apply -f dist/k8s/storage.yaml
kubectl apply -f dist/k8s/config.yaml
kubectl apply -f dist/k8s/opa.yaml
kubectl apply -f dist/k8s/ops.yaml
kubectl wait -n a2a --for=condition=complete job/a2a-migrate --timeout=10m
kubectl apply -f dist/k8s/platform.yaml
```

Espera a que la Ingress cree el ALB y que los pods estén listos; después publica el alias de Route 53:

```sh
kubectl rollout status -n a2a deployment/a2a-api --timeout=10m
deploy/aws/production/publish_dns.sh
```

Terraform limita el endpoint del plano de control EKS a los CIDR administrativos configurados. Ejecuta kubectl desde una VPN o runner permitidos. Para activar MQTT, guarda primero credenciales y CA del broker, renderiza con `--with-iot` y aplica `dist/k8s/iot.yaml`.

## 7. Configurar agentes de Keycloak y ejecutar la comprobación de producción

Importa `deploy/keycloak/realm-a2a.json` en el realm `a2a` antes de desplegar y define el issuer real en `production.env`. Cuando la API ya esté activa, aprovisiona clientes-agente con credenciales temporales de Keycloak o de un cliente provisionador, un token Vault limitado y un token administrador de A2A:

```sh
export KEYCLOAK_URL=https://identity.example.org
export KEYCLOAK_ADMIN_USERNAME='<administrador temporal>'
export KEYCLOAK_ADMIN_PASSWORD='<contraseña temporal>'
export A2A_API_URL=https://agents.example.org
export A2A_PUBLIC_URL=https://agents.example.org
export A2A_ADMIN_BEARER_TOKEN='<token admin A2A de corta duración>'
python -m app.keycloak_provisioning --agent-client-ids a2a-agent-search,a2a-agent-ops
```

El provisionador reconcilia roles y redirects de consola, crea clientes confidenciales con client credentials, guarda sus secretos en Vault y activa únicamente identidades nuevas/en estado provisionado. No reactiva identidades suspendidas o revocadas.

El archivador de auditoría se ejecuta cada cinco minutos y los backups de base de datos son diarios. Después de que exista el primer archivado, verifica el despliegue con el CronJob de preflight suspendido:

```sh
kubectl create job -n a2a --from=cronjob/a2a-production-preflight \
  "a2a-production-preflight-$(date -u +%Y%m%d%H%M%S)"
kubectl wait -n a2a --for=condition=complete \
  -l app=a2a-production-preflight --timeout=10m
kubectl logs -n a2a -l app=a2a-production-preflight --all-containers=true
```

La comprobación valida Keycloak discovery, acceso de runtime a Vault, TLS/migraciones PostgreSQL, TLS Valkey, decisiones OPA permitidas y denegadas, credenciales Langfuse/proveedor y paridad WORM. Ensaya la restauración de backups de base de datos y del data lake en un entorno aislado. La retención Compliance S3 impide borrar permanentemente versiones retenidas; el verificador detecta versiones inesperadas y marcadores de borrado.
