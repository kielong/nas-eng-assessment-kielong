import httpx
import pytest
import respx

from app.vpic import DecodedVehicle, VpicError, _field, decode_vin

BASE_URL = "https://vpic.nhtsa.dot.gov/api"
VIN = "1HGCM82633A004352"
ENDPOINT = f"{BASE_URL}/vehicles/DecodeVinValues/{VIN}"

HONDA = {
    "Make": "HONDA",
    "Model": "Accord",
    "ModelYear": "2003",
    "BodyClass": "Sedan/Saloon",
}


class TestField:
    def test_none_becomes_empty_string(self):
        assert _field({"Make": None}, "Make") == ""

    def test_missing_key_becomes_empty_string(self):
        assert _field({}, "Make") == ""

    def test_non_string_value_is_stringified(self):
        assert _field({"ModelYear": 2003}, "ModelYear") == "2003"

    def test_string_value_passes_through(self):
        assert _field({"Make": "HONDA"}, "Make") == "HONDA"


class TestDecodeVin:
    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_decode_maps_four_fields(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"Results": [HONDA]}))
        async with httpx.AsyncClient() as client:
            decoded = await decode_vin(client, BASE_URL, VIN)
        assert decoded == DecodedVehicle(
            make="HONDA", model="Accord", model_year="2003", body_class="Sedan/Saloon"
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_shape_is_format_json_no_modelyear_param(self):
        route = respx.get(ENDPOINT).mock(
            return_value=httpx.Response(200, json={"Results": [HONDA]})
        )
        async with httpx.AsyncClient() as client:
            await decode_vin(client, BASE_URL, VIN)
        sent = route.calls.last.request.url.params
        assert sent.get("format") == "json"
        assert "modelyear" not in sent

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_error_status_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(500, text="nope"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="request failed"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(side_effect=httpx.TimeoutException("timed out"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="request failed"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_json_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, text="not-json"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="invalid JSON"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_results_list_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"Results": []}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="no decode results"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_results_not_a_list_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"Results": "oops"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="no decode results"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_first_result_not_a_dict_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"Results": ["oops"]}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="no decode results"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_results_key_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"Count": 0}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="no decode results"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_fields_empty_raises_vpic_error(self):
        respx.get(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"Results": [{"Make": None, "Model": None, "ModelYear": None, "BodyClass": None}]},
            )
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(VpicError, match="could not decode"):
                await decode_vin(client, BASE_URL, VIN)

    @pytest.mark.asyncio
    @respx.mock
    async def test_one_field_present_is_still_a_success(self):
        respx.get(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"Results": [{"Make": "HONDA", "Model": None, "ModelYear": None, "BodyClass": None}]},
            )
        )
        async with httpx.AsyncClient() as client:
            decoded = await decode_vin(client, BASE_URL, VIN)
        assert decoded == DecodedVehicle(make="HONDA", model="", model_year="", body_class="")
