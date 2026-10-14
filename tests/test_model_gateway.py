import httpx
from pydantic import SecretStr

from app.config import Settings
from app.embeddings import DIMENSIONS, embedding_for
from app.model_gateway import ModelGateway, ModelResult, gateway_status
from app.usage import aggregate_model_usage


def test_openai_compatible_gateway_records_tokens_and_cost(monkeypatch):
    observed = {}

    def fake_post(url, headers, json, timeout):
        observed.update(url=url, headers=headers, body=json, timeout=timeout)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": "Respuesta verificada"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    config = Settings(
        _env_file=None,
        environment="test",
        model_provider="openai-compatible",
        model_base_url="https://models.example/v1",
        model_name="atlas-test",
        model_api_key=SecretStr("hidden-key"),
        model_input_cost_per_million=2.0,
        model_output_cost_per_million=8.0,
    )

    result = ModelGateway(config).generate("system prompt", "private user prompt")

    assert result.text == "Respuesta verificada"
    assert result.succeeded is True
    assert result.input_tokens == 120
    assert result.output_tokens == 30
    assert result.estimated_cost_usd == 0.00048
    assert observed["url"] == "https://models.example/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer hidden-key"
    assert observed["body"]["max_tokens"] == 1200
    assert "private user prompt" not in str(result.as_dict())
    assert "hidden-key" not in str(result.as_dict())


def test_anthropic_gateway_uses_native_messages_api(monkeypatch):
    observed = {}

    def fake_post(url, headers, json, timeout):
        observed.update(url=url, headers=headers, body=json)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "content": [
                    {"type": "text", "text": "Bloque uno."},
                    {"type": "text", "text": "Bloque dos."},
                ],
                "usage": {"input_tokens": 14, "output_tokens": 9},
            },
        )

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="anthropic",
            model_name="claude-test",
            anthropic_api_key=SecretStr("anthropic-key"),
        )
    )

    result = gateway.generate("system", "question")

    assert result.text == "Bloque uno.Bloque dos."
    assert observed["url"] == "https://api.anthropic.com/v1/messages"
    assert observed["headers"]["x-api-key"] == "anthropic-key"
    assert observed["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in observed["headers"]
    assert observed["body"]["system"] == "system"
    assert result.input_tokens == 14
    assert result.output_tokens == 9


def test_openai_gateway_uses_current_completion_limit_field(monkeypatch):
    observed = {}

    def fake_post(url, headers, json, timeout):
        observed.update(url=url, headers=headers, body=json)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "OpenAI response"}}], "usage": {}},
        )

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    config = Settings(
        _env_file=None,
        environment="test",
        model_provider="openai",
        model_name="gpt-test",
        openai_api_key=SecretStr("openai-key"),
    )

    result = ModelGateway(config).generate("system", "question")

    assert result.text == "OpenAI response"
    assert observed["url"] == "https://api.openai.com/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer openai-key"
    assert observed["body"]["max_completion_tokens"] == 1200
    assert "max_tokens" not in observed["body"]
    assert "temperature" not in observed["body"]


def test_ollama_needs_no_api_key_and_uses_openai_compatible_route(monkeypatch):
    observed = {}

    def fake_post(url, headers, json, timeout):
        observed.update(url=url, headers=headers)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "local"}}], "usage": {}},
        )

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="ollama",
            model_name="qwen3:4b",
            ollama_base_url="http://ollama:11434/v1",
        )
    )

    result = gateway.generate("system", "question")

    assert gateway.configured is True
    assert result.text == "local"
    assert observed["url"] == "http://ollama:11434/v1/chat/completions"
    assert "Authorization" not in observed["headers"]
    assert result.estimated_cost_usd is None


def test_hosted_provider_without_key_is_not_called():
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="openai",
            model_name="gpt-test",
        )
    )

    result = gateway.generate("system", "question")

    assert gateway.configured is False
    assert result.attempted is False
    assert result.fallback_reason == "api_key_not_set"
    assert gateway_status(gateway.config)["secrets_loaded"] is False


def test_provider_http_error_falls_back_without_response_body_or_key(monkeypatch):
    def fake_post(url, headers, json, timeout):
        request = httpx.Request("POST", url)
        response = httpx.Response(401, request=request, text="private provider error hidden-key")
        raise httpx.HTTPStatusError("provider error", request=request, response=response)

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="groq",
            model_name="llama-test",
            groq_api_key=SecretStr("hidden-key"),
        )
    )

    result = gateway.generate("private system prompt", "private user prompt")

    assert result.text is None
    assert result.fallback_reason == "http_401"
    assert "hidden-key" not in str(result.as_dict())
    assert "private" not in str(result.as_dict())


def test_provider_timeout_falls_back_with_safe_reason(monkeypatch):
    def fake_post(url, headers, json, timeout):
        raise httpx.ReadTimeout("private endpoint details")

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="openai-compatible",
            model_base_url="https://models.example/v1",
            model_name="atlas-test",
        )
    )

    result = gateway.generate("system", "question")

    assert result.succeeded is False
    assert result.fallback_reason == "timeout"


def test_invalid_provider_payload_falls_back_without_exposing_content(monkeypatch):
    def fake_post(url, headers, json, timeout):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [], "private": "provider payload"},
        )

    monkeypatch.setattr("app.model_gateway.httpx.post", fake_post)
    gateway = ModelGateway(
        Settings(
            _env_file=None,
            environment="test",
            model_provider="openai-compatible",
            model_base_url="https://models.example/v1",
            model_name="atlas-test",
        )
    )

    result = gateway.generate("system", "question")

    assert result.fallback_reason == "invalid_response"
    assert "provider payload" not in str(result.as_dict())


def test_workflow_persists_model_usage_without_replacing_evidence_controls(client, monkeypatch):
    def fake_generate(self, system_prompt, user_prompt):
        return ModelResult(
            text="Respuesta basada en fuentes autorizadas.",
            provider="openai",
            model="test-model",
            attempted=True,
            succeeded=True,
            input_tokens=40,
            output_tokens=12,
            estimated_cost_usd=0.0001,
        )

    monkeypatch.setattr("app.workflows.engine.ModelGateway.generate", fake_generate)

    response = client.post(
        "/api/v1/tasks",
        json={"scenario": "knowledge", "message": "¿Cómo justifico los gastos de viaje?"},
    )

    assert response.status_code == 201
    result = response.json()["result"]
    assert result["answer"] == "Respuesta basada en fuentes autorizadas."
    assert result["summary_mode"] == "model"
    assert result["model_usage"]["input_tokens"] == 40
    assert result["citations"]
    assert result["evaluation"]["passed"] is True


def test_openai_embeddings_use_provider_secret_and_match_pgvector_dimension(monkeypatch):
    observed = {}

    def fake_post(url, headers, json, timeout):
        observed.update(url=url, headers=headers, body=json)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"data": [{"embedding": [0.25] * DIMENSIONS}]},
        )

    monkeypatch.setattr("app.embeddings.httpx.post", fake_post)
    config = Settings(
        _env_file=None,
        environment="test",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        openai_api_key=SecretStr("embedding-secret"),
    )

    vector = embedding_for("authorized test document", config)

    assert observed["url"] == "https://api.openai.com/v1/embeddings"
    assert observed["headers"]["Authorization"] == "Bearer embedding-secret"
    assert observed["body"]["dimensions"] == DIMENSIONS
    assert len(vector) == DIMENSIONS
    assert vector[0] == 0.25


def test_embedding_provider_without_configuration_keeps_local_fallback():
    config = Settings(_env_file=None, environment="test")

    vector = embedding_for("authorized test document", config)

    assert len(vector) == DIMENSIONS
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_usage_aggregation_excludes_unattempted_tasks_and_keeps_cost_unknown():
    summary = aggregate_model_usage(
        [
            {
                "model_usage": {
                    "provider": "openai",
                    "model": "gpt-test",
                    "attempted": True,
                    "succeeded": True,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "estimated_cost_usd": None,
                }
            },
            {"model_usage": {"provider": "ollama", "attempted": False}},
            {"model_usage": {"provider": "groq", "attempted": True, "succeeded": False}},
        ],
        pricing_configured=True,
    )

    assert summary["calls"] == 2
    assert summary["successful_calls"] == 1
    assert summary["fallback_calls"] == 1
    assert summary["input_tokens"] == 100
    assert summary["output_tokens"] == 20
    assert summary["estimated_cost_usd"] is None
    assert [item["provider"] for item in summary["providers"]] == ["groq", "openai"]


def test_usage_endpoint_is_aggregated_and_does_not_include_task_prompts(client):
    created = client.post(
        "/api/v1/tasks",
        json={"scenario": "incident", "message": "Preparar revisión confidencial."},
    )
    assert created.status_code == 201

    response = client.get("/api/v1/usage")

    assert response.status_code == 200
    data = response.json()
    assert data["sampled_tasks"] == 1
    assert data["calls"] == 0
    assert "Preparar revisión confidencial" not in response.text
    assert "model_api_key" not in response.text


def test_architecture_json_serves_extracted_map_and_guided_flows(client):
    response = client.get("/static/architecture.json")

    assert response.status_code == 200
    architecture = response.json()
    assert len(architecture["nodes"]) == 123
    assert len(architecture["scenarios"]) == 3
    assert len(architecture["connections"]) == 20
