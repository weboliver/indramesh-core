from pydantic_settings import BaseSettings


class StorageServiceSettings(BaseSettings):
    storage_blob_root: str = "/data/storage"
    storage_bucket: str = "indramesh"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = StorageServiceSettings()