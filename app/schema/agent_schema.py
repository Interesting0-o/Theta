from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    content:str

class ReviewRequest(BaseModel):
    type:str