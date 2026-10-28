from pydantic import BaseModel, ConfigDict, Field

from app.validators import EscapedStr, PlainStr


class Token(BaseModel):
    access_token: PlainStr = Field(max_length=2000)
    token_type: PlainStr = Field(max_length=50)


class TokenData(BaseModel):
    user_id: PlainStr | None = Field(default=None, max_length=50)
    username: PlainStr | None = Field(default=None, max_length=100)


class UserResponse(BaseModel):
    id: EscapedStr = Field(max_length=50)
    username: EscapedStr = Field(max_length=100)
    email: EscapedStr = Field(max_length=255)
    is_active: bool
    is_superuser: bool
    
    model_config = ConfigDict(from_attributes=True)