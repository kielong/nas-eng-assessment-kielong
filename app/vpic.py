from dataclasses import dataclass

import httpx


class VpicError(Exception):
    """Raised when NHTSA vPIC cannot be used to decode a VIN."""


@dataclass(frozen=True)
class DecodedVehicle:
    make: str
    model: str
    model_year: str
    body_class: str


def _field(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return str(value)


async def decode_vin(client: httpx.AsyncClient, base_url: str, vin: str) -> DecodedVehicle:
    url = f"{base_url}/vehicles/DecodeVinValues/{vin}"
    try:
        response = await client.get(url, params={"format": "json"})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise VpicError("vPIC request failed") from exc
    except ValueError as exc:
        raise VpicError("vPIC returned invalid JSON") from exc

    results = payload.get("Results") if isinstance(payload, dict) else None
    if not results or not isinstance(results, list) or not isinstance(results[0], dict):
        raise VpicError("vPIC returned no decode results")

    row = results[0]
    decoded = DecodedVehicle(
        make=_field(row, "Make"),
        model=_field(row, "Model"),
        model_year=_field(row, "ModelYear"),
        body_class=_field(row, "BodyClass"),
    )
    if not any((decoded.make, decoded.model, decoded.model_year, decoded.body_class)):
        # vPIC returns HTTP 200 with Results[0] even for a well-formed but
        # undecodable VIN; all four fields blank is that case, confirmed
        # against the live API rather than vPIC's own ErrorCode taxonomy.
        raise VpicError("vPIC could not decode this VIN")
    return decoded
