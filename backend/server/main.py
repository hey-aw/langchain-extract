"""Entry point into the server."""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jsonschema import Draft202012Validator, exceptions
from langchain.chains.openai_functions import create_openai_fn_runnable
from langchain_core.runnables import chain
from langchain_openai.chat_models import ChatOpenAI
from langserve import CustomUserType, add_routes
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session
from typing_extensions import Annotated, TypedDict

from db.models import Extractor, get_session
from extraction.utils import (
    FewShotExample,
    convert_json_schema_to_openai_schema,
    make_prompt_template,
)
from server.api import examples, extractors
from server.validators import validate_json_schema

app = FastAPI(
    title="Extraction Powered by LangChain",
    description="An extraction service powered by LangChain.",
    version="0.0.1",
    openapi_tags=[
        {
            "name": "extraction",
            "description": "Operations related to extracting content from text.",
        }
    ],
)


origins = [
    "http://localhost:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/ready")
def ready():
    return "ok"


# Include API endpoints for extractor definitions
app.include_router(extractors.router)
app.include_router(examples.router)


class ExtractRequest(CustomUserType):
    """Request body for the extract endpoint."""

    text: str = Field(..., description="The text to extract from.")
    json_schema: Dict[str, Any] = Field(
        ...,
        description="JSON schema that describes what content should be extracted "
        "from the text.",
        alias="schema",
    )
    instructions: Optional[str] = Field(
        None, description="Supplemental system instructions."
    )
    examples: Optional[List[FewShotExample]] = Field(
        None, description="Few shot examples."
    )

    @validator("json_schema")
    def validate_schema(cls, v: Any) -> Dict[str, Any]:
        """Validate the schema."""
        validate_json_schema(v)
        return v


class ExtractResponse(BaseModel):
    """Response body for the extract endpoint."""

    extracted: Any


model = ChatOpenAI(temperature=0)


@chain
def extraction_runnable(extraction_request: ExtractRequest) -> ExtractResponse:
    """An end point to extract content from a given text object.

    Used for powering an extraction playground.
    """
    schema = extraction_request.json_schema
    name = schema.get("title", "")
    try:
        Draft202012Validator.check_schema(schema)
    except exceptions.ValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid schema: {e.message}")

    prompt = make_prompt_template(
        extraction_request.instructions, extraction_request.examples, name
    )
    openai_function = convert_json_schema_to_openai_schema(schema)
    runnable = create_openai_fn_runnable(
        functions=[openai_function], llm=model, prompt=prompt
    )
    extracted_content = runnable.invoke({"text": extraction_request.text})

    return ExtractResponse(
        extracted=extracted_content,
    )


class ExtractFromFileRequest(TypedDict):
    """Extract endpoint that uses an existing extractor."""

    extractor_id: Annotated[UUID, "The extractor ID to use."]
    text: Annotated[str, "The text to extract from."]


@app.post("/extract", response_model=ExtractResponse, tags=["extraction"])
async def extract_using_existing_extractor(
    extract_request: ExtractFromFileRequest, *, session: Session = Depends(get_session)
) -> ExtractResponse:
    """Endpoint that is used with an existing extractor.

    This endpoint will be expanded to support upload of binary files as well as
    text files.
    """
    extractor = (
        session.query(Extractor)
        .filter(Extractor.uuid == extract_request["extractor_id"])
        .scalar()
    )
    if extractor is None:
        raise HTTPException(status_code=404, detail="Extractor not found.")

    # Use the json schema and examples
    json_schema = extractor.schema
    examples = extractor.examples
    assert examples == []
    return ExtractResponse(extracted="placeholder")


add_routes(
    app,
    extraction_runnable,
    path="/extract_text",
    enabled_endpoints=["invoke", "playground", "stream_log"],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=8000)
