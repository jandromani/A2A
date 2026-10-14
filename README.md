# Agentic Platform

Implementación ejecutable de la arquitectura del Agentic Atlas: una plataforma común para registrar capacidades, orquestar agentes, conectar herramientas, aplicar permisos, revisar resultados y conservar trazabilidad.

La entrega cubre los tres recorridos descritos en el mapa:

- **Conocimiento corporativo**: búsqueda dentro del alcance del usuario, citas de origen y evaluación de evidencia.
- **Inventario técnico**: lectura segura de archivos de configuración, extracción de declaraciones y vínculo de cada hecho con su archivo.
- **Incidencia operativa**: consulta de Jira si se configura, borrador de actuación y aprobación humana antes de escribir.

La consola web incluye el mapa maestro navegable (123 componentes, decisiones técnicas, criterios de aceptación, conexiones propuestas y tres recorridos guiados). La API funciona en local con SQLite y reglas deterministas. PostgreSQL con pgvector, Valkey Streams, OPA, Keycloak, Jira y proveedores OpenAI, Anthropic, Groq, Ollama u OpenAI-compatible se activan mediante configuración. Las claves se resuelven desde el entorno, archivos montados o Vault y no se guardan en la base de datos.

Además de los tres recorridos de ejemplo, la consola permite crear flujos declarativos por pasos. La memoria conversacional y los checkpoints son persistentes y están aislados por usuario y contexto. El catálogo de fuentes incluye REST/CRM/ERP, archivos CSV/Parquet y recepción de eventos. GitHub y Gmail disponen de conectores propios; toda escritura crea una aprobación visible antes de ejecutarse.

La consola de Sistema permite registrar y sincronizar fuentes REST/CRM/ERP y archivos del lake, buscar incidencias GitHub y solicitar comentarios, buscar y leer correo Gmail y solicitar envíos. Toda escritura externa queda en la cola de aprobación humana. También registra y prueba servidores MCP y agentes A2A remotos; cada llamada queda auditada. En producción, los conectores salientes exigen HTTPS y un hostname incluido en `CONNECTOR_ALLOWED_HOSTS`.

## Ejecutar en local

Requiere Python 3.11 o posterior.

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install -e ".[dev]"
    cp .env.example .env
    uvicorn app.main:app --host 127.0.0.1 --port 8000

Abre http://localhost:8000. La base SQLite se crea en data/agentic.db y carga capacidades y fuentes de ejemplo en el primer arranque. La documentación bajo seed:// está identificada como datos de demostración y puede administrarse desde la API.

## Ejecutar con Compose

    docker compose up --build

La consola queda ligada a 127.0.0.1:8000. Compose inicia PostgreSQL con pgvector y Valkey, aplica la migración inicial y después levanta la aplicación. Las credenciales incluidas son solo para desarrollo local. En despliegues propios configura `ENVIRONMENT`, `PUBLIC_URL`, `AUTH_MODE`, `COMPOSE_DATABASE_URL` y los secretos de PostgreSQL antes de levantar Compose.

Los servicios opcionales se pueden iniciar con perfiles:

    docker compose --profile governance up --build
    docker compose --profile identity up --build
    docker compose --profile agentgateway up --build

Para activar OPA, define OPA_URL=http://opa:8181 al iniciar el perfil governance. Para Keycloak, crea el realm y el cliente OIDC de la organización y configura OIDC_ISSUER_URL y OIDC_AUDIENCE.

El perfil `agentgateway` usa la imagen oficial `cr.agentgateway.dev/agentgateway:v1.6.0` y escucha dentro de Compose en el puerto 4000. Para enrutar el modelo por él, configura `MODEL_PROVIDER=openai-compatible`, `MODEL_BASE_URL=http://agentgateway:4000/v1`, `MODEL_NAME` con el modelo contratado y `OPENAI_API_KEY` en `.env`. La clave del proveedor se entrega al gateway; el tráfico de la aplicación no requiere una clave OpenAI local.

Para Ollama local, configura `MODEL_PROVIDER=ollama`, `MODEL_NAME=qwen3:4b` y `OLLAMA_BASE_URL=http://ollama:11434/v1` en `.env`; luego inicia el perfil y descarga el modelo:

    docker compose --profile local-model up --build -d
    docker compose exec ollama ollama pull qwen3:4b

## Flujos y controles

El motor usa LangGraph para enlazar autorización, ejecución, evaluación, aprobación y registro. Las evaluaciones actuales son deterministas:

- Conocimiento: comprueba que la consulta se restringió a los alcances concedidos y que hay citas.
- Inventario: comprueba procedencia por archivo y deja visible qué ficheros no se procesaron.
- Incidencia: comprueba la fuente y exige aprobación humana para cualquier escritura.

Sin modelo configurado, la respuesta utiliza reglas y evidencia local. Selecciona el proveedor con `MODEL_PROVIDER`. OpenAI, Anthropic y Groq usan sus endpoints oficiales y sus respectivas claves; Ollama usa la URL local configurada. `openai-compatible` conecta cualquier API que implemente chat completions mediante `MODEL_BASE_URL`. La síntesis del modelo no sustituye las evaluaciones ni los controles de acceso. Si el proveedor no está disponible, devuelve un error HTTP, agota el tiempo o responde en un formato inválido, el flujo conserva su resultado de reglas y registra la causa sin guardar el prompt ni la respuesta remota.

La página **Calidad y consumo** agrega el ledger completo de llamadas, sin límite por número de tareas. Cada intento se registra una sola vez con clave idempotente, proveedor/modelo, actor, tokens, tarifas capturadas y coste estimado; SQLite y PostgreSQL rechazan cambios y borrados del ledger. `GET /api/v1/usage/calls?limit=100&cursor=...` devuelve el detalle paginado. El importe es una estimación calculada con las tarifas configuradas en el momento de la llamada; no sustituye la factura del proveedor y queda incompleto si faltan tarifas o datos de tokens.

Si se configuran `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY` y `LANGFUSE_SECRET_KEY`, OpenTelemetry envía spans por OTLP HTTP a Langfuse. Los spans incluyen proveedor, modelo, latencia, uso reportado y coste estimado, sin añadir prompts ni respuestas del usuario a esos atributos. `OTEL_EXPORTER_OTLP_ENDPOINT` se usa como destino alternativo cuando Langfuse no está configurado.

El inspector técnico acepta XML Maven, package.json, Dockerfile, Gradle, go.mod y configuración YAML. Lee el contenido como texto; no ejecuta comandos, builds ni scripts.

Los flujos configurables admiten `knowledge.search`, `model.generate`, `connector.invoke`, `approval` y `result`. Se versionan, guardan entradas y checkpoints por tarea, y continúan después de la aprobación humana. Toda llamada a un conector dentro de un flujo debe ir precedida inmediatamente por un paso de aprobación. La consola incluye un editor JSON y un ejemplo ejecutable. Es un DSL acotado y extensible; no ejecuta código arbitrario ni pretende reemplazar los runtimes específicos de cada caso.

La memoria conversacional conserva hasta `CONVERSATION_MEMORY_TURNS` turnos durante `CONVERSATION_MEMORY_TTL_DAYS`. La consola mantiene un identificador de conversación en el navegador y permite iniciar otra sin borrar la anterior. La API expone lectura y borrado de contexto; otro usuario no puede leerlo.

Las fuentes CRM y ERP usan el adaptador REST configurable, con campos de mapeo, paginación, allowlist de hosts y secretos por nombre de variable de entorno. Data lake admite CSV y Parquet dentro de `DATA_LAKE_ROOT`. Los pipelines pueden enviar lotes JSON por la ruta de eventos; el worker los procesa mediante Valkey Streams y mantiene el outbox SQL para recuperación. En Compose el worker y el modo de ejecución en cola están activados por defecto.

En el flujo de incidencia, dejar la clave de Jira vacía usa un caso sintético local claramente etiquetado. Con JIRA_BASE_URL, JIRA_EMAIL y JIRA_API_TOKEN se leen incidencias reales. La aprobación publica el borrador como comentario Jira. Sin esos tres valores, una clave de Jira se rechaza y no se presenta como una operación completada.

## Interfaces

- REST API y OpenAPI: /docs
- Estado: /health y /ready
- Métricas Prometheus: /metrics
- A2A 1.0 mediante el SDK oficial: /.well-known/agent-card.json y /a2a-sdk/. El adaptador anterior `/a2a` sigue disponible para clientes compatibles.
- MCP Streamable HTTP mediante el SDK oficial: `/mcp-sdk/`. El adaptador anterior `/mcp` mantiene compatibilidad con revisiones MCP publicadas y con clientes existentes.

Ejemplo A2A para crear una consulta de conocimiento:

    curl -X POST http://localhost:8000/a2a \
      -H "A2A-Version: 1.0" \
      -H "Content-Type: application/json" \
      -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"msg-1","role":"ROLE_USER","parts":[{"text":"¿Cómo justifico un gasto de viaje?"}]}}}'

La respuesta contiene un task id, estado, resultado estructurado y referencias a las fuentes. En un despliegue protegido, envía el mismo bearer token que usarías con la API REST.

## API principal

| Método | Ruta | Uso |
| --- | --- | --- |
| GET | /api/v1/dashboard | Indicadores operativos |
| GET | /api/v1/catalog | Agentes, flujos y herramientas |
| GET/POST | /api/v1/knowledge | Consultar o añadir documentos |
| DELETE | /api/v1/knowledge/{id} | Desactivar una fuente |
| GET/POST | /api/v1/tasks | Consultar o ejecutar tareas |
| GET | /api/v1/tasks/{id}/events | Historial del recorrido |
| POST | /api/v1/approvals/{id}/decision | Aprobar o rechazar un borrador |
| GET/POST | /api/v1/workflows | Listar, crear o actualizar flujos declarativos |
| POST | /api/v1/workflows/{id}/runs | Ejecutar un flujo versionado |
| GET/DELETE | /api/v1/conversations/{context_id} | Leer o borrar memoria propia |
| GET/POST | /api/v1/data-sources | Registrar y consultar fuentes REST, CRM, ERP o data lake |
| POST | /api/v1/data-sources/{id}/sync | Sincronizar una fuente |
| POST | /api/v1/data-sources/{id}/events | Ingerir un lote de pipeline |
| GET | /api/v1/integrations/github/{owner}/{repo}/issues | Leer incidencias GitHub |
| POST | /api/v1/integrations/github/{owner}/{repo}/issues/{number}/comments | Solicitar comentario GitHub, sujeto a aprobación |
| GET | /api/v1/integrations/gmail/search | Buscar correo Gmail |
| GET | /api/v1/integrations/gmail/messages/{id} | Leer un mensaje Gmail |
| POST | /api/v1/integrations/gmail/messages | Solicitar envío, sujeto a aprobación |
| GET | /api/v1/evaluations | Resultados de evaluación |
| GET | /api/v1/usage | Consumo agregado de modelos, tokens y coste estimado |
| GET | /api/v1/usage/calls | Ledger de llamadas paginado por cursor |
| GET | /api/v1/activity | Auditoría de acciones |
| GET | /api/v1/audit/verify | Verificación de la cadena de hashes |
| GET | /api/v1/tasks/{id}/replay | Replay de solo lectura de eventos y auditoría |
| GET/POST | /api/v1/agent-identities | Registro y consulta de identidades de agentes |
| POST | /api/v1/agent-identities/{client_id}/{action} | Cambio auditado: `activate`, `suspend` o `revoke` |
| GET | /api/v1/system | Estado sin revelar secretos |
| GET/POST | /api/v1/connectors | Listar o registrar conectores A2A/MCP |
| DELETE | /api/v1/connectors/{id} | Desactivar un conector |
| POST | /api/v1/connectors/{id}/enable | Reactivar un conector |
| GET | /api/v1/connectors/{id}/tools | Consultar herramientas de un servidor MCP |
| POST | /api/v1/connectors/{id}/invoke | Invocar un agente A2A o herramienta MCP autorizada |

Las tareas conservan petición, resultado, estado, evaluación y eventos. El registro de auditoría encadena cada entrada con SHA-256 y bloquea `UPDATE` y `DELETE` en SQLite y PostgreSQL; `/api/v1/audit/verify` comprueba la secuencia y detecta modificaciones. El replay devuelve una proyección ordenada de eventos y auditoría, y **no vuelve a ejecutar efectos externos**. Esta protección es append-only a nivel de base de datos: un administrador con privilegios para retirar triggers o restaurar la base puede eludirla; para retención WORM hay que activar Object Lock/versionado y permisos de solo escritura en el almacén externo.

## Configuración de seguridad

El modo local usa AUTH_MODE=dev y crea un administrador de desarrollo. Solo es apropiado para ejecución local. La configuración de Compose publica el puerto en loopback. `AUTH_MODE=api_key` asigna la identidad y permisos definidos por `API_KEY_SUBJECT`, `API_KEY_ROLES` y `API_KEY_SCOPES`; por defecto el token tiene rol `employee`, nunca `admin`.

Para producción:

1. Define `ENVIRONMENT=production`.
2. Importa `deploy/keycloak/realm-a2a.json` en el realm `a2a` (Compose lo importa con el perfil `identity`). El realm define `employee`, `operator`, `approver`, `admin` y `agent`; clientes de API, consola web y plantilla de agentes; y el mapper de audiencia `a2a-api`. Ajusta redirect URIs y dominios para tu entorno.
3. Provisiona agentes con `python -m app.keycloak_provisioning --agent-client-ids a2a-agent-search,a2a-agent-ops`. La herramienta crea clientes confidenciales con service account, client credentials, audiencia `a2a-api` y rol `agent`; copia cada secreto directamente a Vault KV v2 y no lo imprime. Requiere credenciales temporales de un administrador de Keycloak y un token Vault con permiso limitado de escritura en la ruta indicada. Registra y activa cada `client_id` con las rutas administrativas de la API. El `azp`/`client_id` del token debe coincidir exactamente con el registro; la API vuelve a comprobar estado y expiración en cada petición.
4. Define `OIDC_ISSUER_URL` y `OIDC_AUDIENCE=a2a-api`. La API valida firma, issuer, audiencia y expiración. Para usuarios, los permisos de documento se expresan mediante `agentic_scopes`; los scopes de agente se almacenan en su registro.
5. Para autorización externa configura `OPA_URL` y despliega `deploy/policy/agentic.rego` con `deploy/k8s/opa.yaml`. Si OPA está configurado y no responde, la plataforma deniega la operación.
6. Usa `SECRET_PROVIDER=vault`, Vault KV v2 y autenticación Kubernetes/AppRole. `deploy/vault/configure_kubernetes_auth.py` crea políticas independientes para API, migraciones, backups y archivado de auditoría. Los campos sensibles aceptan `vault://a2a/production/providers/model#api_key` o `env://NOMBRE`; también se admiten archivos montados en `/run/secrets`. Las credenciales usadas para autenticarse contra Vault deben venir de la identidad del runtime, no de un secreto que Vault tenga que desbloquear a sí mismo.
7. Usa PostgreSQL y Valkey privados con TLS y credenciales propias. No uses en producción los perfiles locales de Vault, Mosquitto anónimo ni las contraseñas de ejemplo.

No se permite iniciar con ENVIRONMENT=production y AUTH_MODE=dev. La consola admite bearer token para una instalación protegida.

Las rutas de identidad son `GET/POST /api/v1/agent-identities` y `POST /api/v1/agent-identities/{client_id}/{activate|suspend|revoke}`. Solo un administrador puede gestionarlas. La revocación es terminal; cada cambio queda auditado.

## Canales y operación

- **PWA web**: `/` sirve un manifiesto instalable, icono, service worker y shell sin conexión. Las llamadas a la API siguen necesitando red.
- **MQTT/IoT**: `MQTT_BROKER_URL=mqtts://...` habilita el subscriber QoS 1. Mensajes JSON con `event_id` se deduplican y se escriben en el outbox SQL antes de confirmar al broker; los mensajes inválidos también deben quedar auditados antes de confirmarse. `deploy/mosquitto/mosquitto-dev.conf` es exclusivamente local. Para producción usa `deploy/mosquitto/mosquitto-prod.conf.example`, TLS, fichero de contraseñas y ACL por dispositivo; crea `a2a-mqtt-secrets` y, si el broker usa CA privada, `a2a-mqtt-tls` con `ca.crt`.
- **Eventos**: el outbox SQL es la fuente durable; Valkey Streams distribuye eventos y los workers los consumen con grupo y confirmación posterior. En Kubernetes las API no consumen eventos dentro del proceso y `a2a-event-worker` escala por separado.
- **Observabilidad**: `/metrics`, OpenTelemetry y Langfuse se activan por configuración. `/api/v1/system` indica si Vault/Langfuse están configurados, sin devolver credenciales.

## Despliegue de producción

`deploy/aws/production` crea una VPC multi-AZ, EKS, PostgreSQL RDS Multi-AZ con pgvector, Valkey Multi-AZ con TLS, EFS RWX, backup de EFS, repositorios ECR privados y buckets S3 separados para backups y auditoría con Object Lock COMPLIANCE, KMS e IAM limitado por workload. `deploy/k8s` contiene configuración, PVC EFS, OPA, ingress/TLS, API, workers, migración, backups y archivado WORM. Consulta [la guía de despliegue AWS](deploy/aws/production/README.md) para aprovisionar el estado remoto, publicar imágenes, inicializar Vault, renderizar manifiestos y ejecutar la puerta de producción. Los secretos y los valores específicos de cada cuenta se quedan fuera del repositorio.

El Job de migración es `a2a-migrate`; espera a que termine correctamente antes de dirigir tráfico al API. El CronJob `a2a-postgres-backup` valida el archivo con `pg_restore --list`, publica el dump custom-format, su SHA-256 y el archivo del lake. `a2a-audit-archive` valida la cadena SQL y archiva cada entrada con retención Object Lock COMPLIANCE; también detecta versiones duplicadas y marcadores de borrado. Ejecuta el CronJob suspendido `a2a-production-preflight` tras el primer archivado para comprobar Keycloak, Vault, PostgreSQL, Valkey, OPA, Langfuse y paridad WORM. Restaura copias periódicamente en un entorno aislado, ejecuta `alembic upgrade head`, `/ready`, flujos de prueba y verifica acceso a los objetos.

## Variables opcionales

- MODEL_PROVIDER: `openai-compatible`, `openai`, `anthropic`, `groq` u `ollama`.
- MODEL_BASE_URL y MODEL_NAME: URL (para `openai-compatible` o para sobrescribir una URL predeterminada) y nombre del modelo.
- MODEL_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY y GROQ_API_KEY: claves de proveedor; el campo específico tiene prioridad sobre MODEL_API_KEY.
- MODEL_MAX_TOKENS: límite de salida solicitado al proveedor.
- MODEL_INPUT_COST_PER_MILLION y MODEL_OUTPUT_COST_PER_MILLION: tarifas en USD por millón de tokens para estimar consumo.
- OLLAMA_BASE_URL: URL compatible con OpenAI de Ollama; dentro de Compose usa `http://ollama:11434/v1`.
- EMBEDDING_PROVIDER, EMBEDDING_BASE_URL, EMBEDDING_MODEL y EMBEDDING_API_KEY: proveedor `openai`, `ollama` u `openai-compatible`; se solicitan vectores de 384 dimensiones para pgvector. OpenAI puede reutilizar OPENAI_API_KEY. Sin servicio externo, la indexación usa hashing local y la recuperación conserva una puntuación lexical.
- DATABASE_URL: SQLite local o PostgreSQL.
- COMPOSE_DATABASE_URL: base de datos que usará la aplicación cuando se despliegue con Compose. Por defecto apunta al PostgreSQL local.
- REDIS_URL: publicación de eventos en streams Valkey; la copia duradera también queda en SQL.
- OPA_URL: decisión de política externa.
- OIDC_ISSUER_URL y OIDC_AUDIENCE: validación de JWT con las claves públicas del issuer.
- API_KEY_SUBJECT, API_KEY_ROLES y API_KEY_SCOPES: identidad de mínimo privilegio para `AUTH_MODE=api_key`.
- AGENT_IDENTITY_CLAIM: claim OIDC opcional para vincular el token con el registro (por defecto `agentic_agent_id`; si falta se usa `azp`/`client_id` para tokens con rol `agent`).
- SECRET_PROVIDER, VAULT_ADDR, VAULT_TOKEN, VAULT_ROLE_ID, VAULT_SECRET_ID, VAULT_KUBERNETES_ROLE, VAULT_KUBERNETES_MOUNT, VAULT_NAMESPACE, VAULT_KV_MOUNT y VAULT_CACHE_TTL_SECONDS: Vault KV v2 y autenticación.
- LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY y LANGFUSE_SECRET_KEY: exportación OTLP HTTP de trazas a Langfuse.
- JIRA_BASE_URL, JIRA_EMAIL y JIRA_API_TOKEN: lectura y comentario de incidencias Jira Cloud.
- GITHUB_TOKEN: token de permisos mínimos para comentarios GitHub aprobados; la lectura de repositorios públicos no requiere token. Las scopes locales se expresan como `github:owner/repository`.
- GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET y GOOGLE_REFRESH_TOKEN: credenciales OAuth de Gmail. Para leer mensajes se necesita `gmail.readonly` y el alcance de plataforma `gmail:read`; para enviar, `gmail.send` y `gmail:send`. El primer permiso es restringido y la publicación de una aplicación puede exigir revisión de OAuth de Google. Usa solo los alcances que necesites.
- DATA_LAKE_ROOT y DATA_SOURCE_SYNC_INTERVAL_SECONDS: directorio de archivos CSV/Parquet y frecuencia de sincronización automática opcional.
- EVENT_WORKER_ENABLED, TASK_EXECUTION_MODE, EVENT_CONSUMER_GROUP y EVENT_CONSUMER_NAME: consumo duradero del outbox SQL o Valkey Streams. Compose los activa y ejecuta en cola por defecto.
- MQTT_BROKER_URL, MQTT_TOPIC_FILTER, MQTT_CLIENT_ID, MQTT_USERNAME, MQTT_PASSWORD, MQTT_CA_FILE y MQTT_MAX_MESSAGE_BYTES: ingesta autenticada de IoT. En producción el broker debe usar `mqtts://` y credenciales.
- OTEL_EXPORTER_OTLP_ENDPOINT: exportador de trazas OpenTelemetry.
- CONNECTOR_ALLOWED_HOSTS: lista separada por comas de hostnames HTTPS autorizados para conectores salientes en producción.

## Verificación

    ruff check app tests migrations
    pytest --cov=app --cov-report=term-missing
    node --check app/static/app.js

El workflow de GitHub Actions ejecuta estas comprobaciones con Python 3.11 y 3.12.
