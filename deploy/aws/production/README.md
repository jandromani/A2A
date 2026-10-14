# Activación de producción

La plataforma incluye el código, Terraform y los manifiestos para AWS/EKS, PostgreSQL/pgvector, Valkey, almacenamiento EFS/S3, copias, archivo WORM, OPA, Vault, Keycloak y Langfuse. El despliegue se ejecuta con **un solo archivo de entrada y un comando**; no hace falta mantener otra configuración paralela de Kubernetes.

## Preparación única

Se necesita una cuenta AWS autorizada, una hosted zone pública de Route 53, un realm o instancia administrable de Keycloak, un Vault con KV v2, un proyecto de Langfuse y una credencial de proveedor de modelos. El principal AWS debe poder crear los recursos definidos en Terraform y el CIDR de administración debe permitir acceso al endpoint de EKS. El rol de Vault debe poder habilitar/configurar Kubernetes auth y KV v2, escribir políticas/roles y guardar los secretos de esta instalación.

Instala Terraform 1.10+, AWS CLI, Docker, kubectl, Helm y las dependencias Python del proyecto. Inicia sesión en AWS con SSO o un rol asumido; no pongas claves AWS de larga duración en el archivo de entrada.

```sh
cp deploy/aws/production/production.env.example deploy/aws/production/production.env
chmod 600 deploy/aws/production/production.env
```

Completa ese archivo con la región, dominio/hosted-zone, CIDR de administración, URLs externas, usuarios administrador existentes de Keycloak y credenciales. `MODEL_BASE_URL` debe ser la base HTTPS de una API de chat compatible con OpenAI (por ejemplo, `https://api.openai.com/v1`); `EMBEDDING_BASE_URL` debe aceptar la API de embeddings compatible con OpenAI. Los nombres de bucket de auditoría y backups se generan a partir de la cuenta y región si se dejan vacíos. Las contraseñas de PostgreSQL y Valkey deben ser aleatorias y cumplir las longitudes indicadas.

Los campos de Jira, GitHub, Gmail y MQTT son opcionales. Deja vacíos todos los campos de una integración que no vayas a activar. MQTT requiere `mqtts://`, usuario, contraseña y la ruta local a la CA del broker. El archivo es leído como datos, nunca como código de shell, y el activador fuerza permisos `0600`.

## Validar y desplegar

Primero valida el archivo y las herramientas instaladas:

```sh
python deploy/aws/production/activate.py
```

Después de comprobar la cuenta AWS activa y los valores, despliega:

```sh
python deploy/aws/production/activate.py --apply
```

`--apply` crea recursos AWS con coste y aplica Terraform. El proceso:

1. Crea un bucket versionado y cifrado para los estados Terraform, y migra allí el estado del bootstrap.
2. Despliega la VPC, EKS, PostgreSQL Multi-AZ, Valkey, EFS, buckets con Object Lock, IAM y ECR.
3. Construye y publica imágenes con tag inmutable basado en el commit limpio.
4. Instala los controladores ALB/EBS/EFS, configura el TokenReview de Vault, crea sus roles Kubernetes y guarda en Vault las URLs de PostgreSQL/Valkey y las credenciales configuradas.
5. Ejecuta dentro de EKS el bootstrap de PostgreSQL con el rol `a2a_runtime`, aplica migraciones y despliega API, los gateways de modelos y A2A/MCP, workers, OPA, backups y archivador WORM. El ALB público enruta a través del gateway, que reenvía a los servicios privados; los tokens OIDC se validan en la API.
6. Importa el realm A2A si aún no existe, ajusta el redirect de la consola, otorga el rol administrador a los usuarios indicados y aprovisiona clientes-agente con el rol limitado `identity-manager`.
7. Publica DNS, comprueba HTTPS, `/ready` y la Agent Card, fuerza un archivo WORM inicial y ejecuta el preflight de servicios.

El bootstrap del usuario PostgreSQL se ejecuta en un Job dentro de la red privada; las credenciales administrativas nunca se exponen al cliente local. Los secretos de proveedores y las contraseñas de infraestructura no se escriben en ConfigMaps ni se imprimen en consola. Los planes y archivos temporales quedan bajo `.secrets/`, que está excluido de Git.

## Comprobaciones y límites operativos

El preflight revisa descubrimiento OIDC, autenticación/lectura de Vault, versión de esquema y conexión TLS de PostgreSQL, Valkey, decisiones permitidas/denegadas en OPA, una exportación OTLP real a Langfuse, una llamada de prueba al modelo y paridad del archivo WORM. La primera activación crea una copia PostgreSQL, comprueba el checksum y la restaura en una base aislada antes de marcarse como completada. Un CronJob semanal repite esa restauración en un pod efímero con volumen EBS cifrado y sin conectividad a la base de producción. El activador no restaura nunca sobre producción.

La retención COMPLIANCE de S3 detecta cambios/borrados y protege el archivo frente a administradores de la base de datos. El backup de PostgreSQL y la restauración automatizada comprueban disponibilidad y checksum del último dump; el backup de EFS conserva los datos del lake mediante AWS Backup. Los secretos operativos permanecen fuera del repositorio. La cuenta AWS, el dominio y zona DNS, las instancias de Keycloak/Vault/Langfuse, las credenciales y el CIDR aprobado pertenecen a la organización y se aportan en el archivo de entrada.

Para rotar secretos, actualiza Vault y vuelve a ejecutar la activación desde un checkout limpio del nuevo commit. El cache de credenciales del proceso expira en 60 segundos; los cambios de imagen usan un nuevo Job de migración por release.
