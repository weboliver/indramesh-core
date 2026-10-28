from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    environment: str = "production"
    graphql_max_depth: int = Field(default=15, ge=1)
    graphql_max_tokens: int = Field(default=2000, ge=1)
    graphql_max_aliases: int = Field(default=20, ge=1)
    graphql_max_cost: int = Field(default=20000, ge=1)
    graphql_nested_list_limit: int = Field(default=500, ge=1)
    graphql_statement_timeout: int = Field(default=5000, ge=1)
    graphql_execution_timeout: float = Field(default=10.0, gt=0)
    aql_ref_cutoff: int = Field(default=10000, ge=1)
    aql_multi_type_cutoff: int = Field(default=25, ge=1)
    object_type_graph_cutoff: int = Field(default=50, ge=1)
    object_graph_cutoff: int = Field(default=50, ge=1)

    model_config = {"env_prefix": "", "case_sensitive": False}

    @model_validator(mode="after")
    def _wall_clock_must_exceed_the_statement_timeout(self) -> "AppSettings":
        """Reject a pairing that would make one of the two timeout sources dead.

        Both bounds are read at call time and the documented relationship
        (``.env.example`` and ``docs/CONFIGURATION.md``) is "the wall clock must
        exceed the statement timeout" — but nothing enforced it, so a wall
        clock at or below the statement timeout (in milliseconds) started
        silently with the Postgres bound as the only rejection that can ever
        fire. An operator would see ``source: wall_clock`` for everything and
        have to read the code to find out why. Invalid settings already fail
        loudly at startup; this one now does too.
        """
        if self.graphql_execution_timeout * 1000 <= self.graphql_statement_timeout:
            raise ValueError(
                "GRAPHQL_EXECUTION_TIMEOUT (seconds) must exceed "
                "GRAPHQL_STATEMENT_TIMEOUT (milliseconds); got "
                f"{self.graphql_execution_timeout} s <= "
                f"{self.graphql_statement_timeout} ms"
            )
        return self


class StorageSettings(BaseSettings):
    storage_service_url: str = "http://storage:8001"
    storage_bucket: str = "indramesh"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = AppSettings()
storage_settings = StorageSettings()
