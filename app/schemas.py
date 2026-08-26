from pydantic import BaseModel, Field, field_validator

VIN_PATTERN = r"^[A-Z0-9]{17}$"


class VinRequest(BaseModel):
    vin: str = Field(min_length=17, max_length=17, pattern=VIN_PATTERN)

    @field_validator("vin", mode="before")
    @classmethod
    def normalize_vin(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class LookupResponse(BaseModel):
    vin: str
    make: str
    model: str
    model_year: str
    body_class: str
    cached: bool


class RemoveResponse(BaseModel):
    vin: str
    deleted: bool
