from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field

# --- Consolidating Part Types ---

class FunctionCall(BaseModel):
    id: Optional[str] = None
    name: str
    args: Dict[str, Any]

class FunctionResponse(BaseModel):
    id: Optional[str] = None
    name: str
    response: Dict[str, Any]


class InlineData(BaseModel):
    data: Optional[str] = None
    mimeType: Optional[str] = None
    displayName: Optional[str] = None


class FileData(BaseModel):
    fileUri: Optional[str] = None
    mimeType: Optional[str] = None
    displayName: Optional[str] = None


class Part(BaseModel):
    text: Optional[str] = None
    functionCall: Optional[FunctionCall] = None
    functionResponse: Optional[FunctionResponse] = None
    inlineData: Optional[InlineData] = None
    fileData: Optional[FileData] = None
    
    # Simple alias for JSON field mapping if needed, 
    # but Pydantic usually handles this with field aliases if inputs differ.
    # For now we stick to the TS interface names.

class GenAiContent(BaseModel):
    role: str = "user"
    parts: List[Part]

class NewMessage(BaseModel):
    parts: List[Part]
    role: str = "user"

class AgentRunRequest(BaseModel):
    appName: str
    userId: str
    sessionId: Optional[str] = None
    newMessage: NewMessage
    streaming: bool = False
    invocationId: Optional[str] = None
    stateDelta: Optional[Dict[str, Any]] = None
    functionCallEventId: Optional[str] = None
    model: Optional[str] = None

# --- Response Types ---

class LlmResponse(BaseModel):
    content: Optional[GenAiContent] = None
    error: Optional[str] = None

class Session(BaseModel):
    id: str
    userId: str
    appName: str
    createdTime: Optional[float] = None
    updatedTime: Optional[float] = None
    
class AppListResponse(BaseModel):
    apps: List[str]
