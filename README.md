# Agentic Platform

Implementación ejecutable de la arquitectura del Agentic Atlas: una plataforma común para registrar capacidades, orquestar agentes, conectar herramientas, aplicar permisos, revisar resultados y conservar trazabilidad.

La entrega cubre los tres recorridos descritos en el mapa:

- **Conocimiento corporativo**: búsqueda dentro del alcance del usuario, citas de origen y evaluación de evidencia.
- **Inventario técnico**: lectura segura de archivos de configuración, extracción de declaraciones y vínculo de cada hecho con su archivo.
- **Incidencia operativa**: consulta de Jira si se configura, borrador de actuación y aprobación humana antes de escribir.

La consola web incluye el mapa maestro navegable (123 componentes, decisiones técnicas, criterios de aceptación, conexiones propuestas y tres recorridos guiados). La API funciona en local con SQLite y reglas deterministas. PostgreSQL con pgvector, Valkey Streams, OPA, Keycloak, Jira y proveedores OpenAI, Anthropic, Groq, Ollama u OpenAI-compatible se activan mediante configuración. Las claves se leen del entorno y no se guardan en la base de datos.

La consola de Sistema también registra y prueba servidores MCP y agentes A2A remotos. Cada llamada remota queda auditada. En producción, los conectores salientes exigen HTTPS y un hostname incluido en `CONNECTOR_ALLOWED_HOSTS`.

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

Para activar OPA, define OPA_URL=http://opa:8181 al iniciar el perfil governance. Para Keycloak, crea el realm y el cliente OIDC de la organización y configura OIDC_ISSUER_URL y OIDC_AUDIENCE.

Para Ollama local, configura `MODEL_PROVIDER=ollama`, `MODEL_NAME=qwen3:4b` y `OLLAMA_BASE_URL=http://ollama:11434/v1` en `.env`; luego inicia el perfil y descarga el modelo:

    docker compose --profile local-model up --build -d
    docker compose exec ollama ollama pull qwen3:4b

## Flujos y controles

El motor usa LangGraph para enlazar autorización, ejecución, evaluación, aprobación y registro. Las evaluaciones actuales son deterministas:

- Conocimiento: comprueba que la consulta se restringió a los alcances concedidos y que hay citas.
- Inventario: comprueba procedencia por archivo y deja visible qué ficheros no se procesaron.
- Incidencia: comprueba la fuente y exige aprobación humana para cualquier escritura.

Sin modelo configurado, la respuesta utiliza reglas y evidencia local. Selecciona el proveedor con `MODEL_PROVIDER`. OpenAI, Anthropic y Groq usan sus endpoints oficiales y sus respectivas claves; Ollama usa la URL local configurada. `openai-compatible` conecta cualquier API que implemente chat completions mediante `MODEL_BASE_URL`. La síntesis del modelo no sustituye las evaluaciones ni los controles de acceso. Si el proveedor no está disponible, devuelve un error HTTP, agota el tiempo o responde en un formato inválido, el flujo conserva su resultado de reglas y registra la causa sin guardar el prompt ni la respuesta remota.

La página **Calidad y consumo** muestra llamadas, fallbacks, tokens informados por el proveedor y coste estimado agregado por proveedor/modelo sobre las últimas 1.000 tareas. El coste solo se calcula si se configuran las tarifas de entrada y salida por millón de tokens; cuando una respuesta no informa tokens, el coste se marca como incompleto.

El inspector técnico acepta XML Maven, package.json, Dockerfile, Gradle, go.mod y configuración YAML. Lee el contenido como texto; no ejecuta comandos, builds ni scripts.

En el flujo de incidencia, dejar la clave de Jira vacía usa un caso sintético local claramente etiquetado. Con JIRA_BASE_URL, JIRA_EMAIL y JIRA_API_TOKEN se leen incidencias reales. La aprobación publica el borrador como comentario Jira. Sin esos tres valores, una clave de Jira se rechaza y no se presenta como una operación completada.

## Interfaces

- REST API y OpenAPI: /docs
- Estado: /health y /ready
- Métricas Prometheus: /metrics
- A2A 1.0: /.well-known/agent-card.json y /a2a. Implementa SendMessage, GetTask y CancelTask sobre JSON-RPC 2.0, y conserva alias para clientes 0.3.
- MCP Streamable HTTP sin estado: /mcp. Implementa la revisión 2026-07-28 con metadatos y cabeceras por petición, además de compatibilidad de servidor con 2025-11-25, 2025-06-18, 2025-03-26 y 2024-11-05. Incluye tools/list, tools/call, resources/list y resources/read; GET devuelve 405 porque el servidor no inicia flujos SSE.

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
| GET | /api/v1/evaluations | Resultados de evaluación |
| GET | /api/v1/usage | Consumo agregado de modelos, tokens y coste estimado |
| GET | /api/v1/activity | Auditoría de acciones |
| GET | /api/v1/system | Estado sin revelar secretos |
| GET/POST | /api/v1/connectors | Listar o registrar conectores A2A/MCP |
| DELETE | /api/v1/connectors/{id} | Desactivar un conector |
| POST | /api/v1/connectors/{id}/enable | Reactivar un conector |
| GET | /api/v1/connectors/{id}/tools | Consultar herramientas de un servidor MCP |
| POST | /api/v1/connectors/{id}/invoke | Invocar un agente A2A o herramienta MCP autorizada |

Las tareas conservan petición, resultado, estado, evaluación y eventos. El registro de auditoría guarda actor, acción, recurso y decisión. Los documentos pueden desactivarse sin borrar su historial.

## Configuración de seguridad

El modo local usa AUTH_MODE=dev y crea un administrador de desarrollo. Solo es apropiado para ejecución local. La configuración de Compose publica el puerto en loopback.

Para producción:

1. Define ENVIRONMENT=production.
2. Usa AUTH_MODE=oidc con el issuer y audience de Keycloak, o AUTH_MODE=api_key con un token fuerte gestionado por un secret manager.
3. Configura los roles employee, operator, approver y admin. Los permisos de documentos se expresan mediante el claim agentic_scopes.
4. Opcionalmente configura OPA_URL; si OPA está configurado pero deja de responder, la autorización falla cerrada.
5. Usa PostgreSQL, Valkey con controles de red y TLS, y secretos de integración inyectados por el entorno de ejecución.

No se permite iniciar con ENVIRONMENT=production y AUTH_MODE=dev. La consola admite bearer token para una instalación protegida.

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
- JIRA_BASE_URL, JIRA_EMAIL y JIRA_API_TOKEN: lectura y comentario de incidencias Jira Cloud.
- OTEL_EXPORTER_OTLP_ENDPOINT: exportador de trazas OpenTelemetry.
- CONNECTOR_ALLOWED_HOSTS: lista separada por comas de hostnames HTTPS autorizados para conectores salientes en producción.

## Verificación

    ruff check app tests migrations
    pytest --cov=app --cov-report=term-missing
    node --check app/static/app.js

El workflow de GitHub Actions ejecuta estas comprobaciones con Python 3.11 y 3.12.
