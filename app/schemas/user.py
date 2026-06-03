from datetime import datetime

from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str


class UserResponse(BaseModel):
    username: str
    api_key: str


class UserList(BaseModel):
    username: str
    active: bool
    created_at: datetime


class SetKeyRequest(BaseModel):
    key: str
