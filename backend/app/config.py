from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_db: str = "yuncunchu"

    redis_host: str = "localhost"
    redis_port: int = 6379

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "files"

    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    faiss_user_index_dir: str = "/data/faiss/users"

    conversation_ttl_seconds: int = 3600
    max_context_messages: int = 20
    max_tool_calls_per_turn: int = 5

    model_config = {"env_file": ".env"}


settings = Settings()
