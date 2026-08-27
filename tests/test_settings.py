import pytest

from app.settings import (
    DEFAULT_CACHE_MAX_ROWS,
    DEFAULT_CACHE_SWEEP_INTERVAL_SECONDS,
    DEFAULT_DATABASE_PATH,
    DEFAULT_TTL_SECONDS,
    DEFAULT_VPIC_BASE_URL,
    DEFAULT_VPIC_TIMEOUT_SECONDS,
    get_settings,
)

ENV_VARS = [
    "CACHE_TTL_SECONDS",
    "DATABASE_PATH",
    "VPIC_BASE_URL",
    "VPIC_TIMEOUT_SECONDS",
    "CACHE_SWEEP_INTERVAL_SECONDS",
    "CACHE_MAX_ROWS",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # get_settings() reads directly from os.environ, not from a fixture the
    # test controls -- an ambient value from the real shell (or leftover from
    # another test) would silently change what "default" means here.
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_defaults_when_nothing_is_set(self):
        settings = get_settings()
        assert settings.cache_ttl_seconds == DEFAULT_TTL_SECONDS
        assert settings.database_path == DEFAULT_DATABASE_PATH
        assert settings.vpic_base_url == DEFAULT_VPIC_BASE_URL
        assert settings.vpic_timeout_seconds == DEFAULT_VPIC_TIMEOUT_SECONDS
        assert settings.cache_sweep_interval_seconds == DEFAULT_CACHE_SWEEP_INTERVAL_SECONDS
        assert settings.cache_max_rows == DEFAULT_CACHE_MAX_ROWS


class TestOverrides:
    def test_every_env_var_overrides_its_field(self, monkeypatch):
        monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
        monkeypatch.setenv("DATABASE_PATH", "/tmp/custom.db")
        monkeypatch.setenv("VPIC_BASE_URL", "https://example.test/api/")
        monkeypatch.setenv("VPIC_TIMEOUT_SECONDS", "2.5")
        monkeypatch.setenv("CACHE_SWEEP_INTERVAL_SECONDS", "30")
        monkeypatch.setenv("CACHE_MAX_ROWS", "5")

        settings = get_settings()

        assert settings.cache_ttl_seconds == 60
        assert settings.database_path == "/tmp/custom.db"
        assert settings.vpic_base_url == "https://example.test/api"  # trailing slash stripped
        assert settings.vpic_timeout_seconds == 2.5
        assert settings.cache_sweep_interval_seconds == 30
        assert settings.cache_max_rows == 5


class TestValidation:
    @pytest.mark.parametrize(
        "env_var", ["CACHE_TTL_SECONDS", "CACHE_SWEEP_INTERVAL_SECONDS", "CACHE_MAX_ROWS"]
    )
    @pytest.mark.parametrize("bad_value", ["0", "-1"])
    def test_zero_or_negative_int_settings_are_rejected(self, monkeypatch, env_var, bad_value):
        monkeypatch.setenv(env_var, bad_value)
        with pytest.raises(ValueError, match=env_var):
            get_settings()

    @pytest.mark.parametrize("bad_value", ["0", "-1.5"])
    def test_zero_or_negative_vpic_timeout_is_rejected(self, monkeypatch, bad_value):
        monkeypatch.setenv("VPIC_TIMEOUT_SECONDS", bad_value)
        with pytest.raises(ValueError, match="VPIC_TIMEOUT_SECONDS"):
            get_settings()

    def test_non_numeric_value_still_fails_fast(self, monkeypatch):
        monkeypatch.setenv("CACHE_TTL_SECONDS", "not-a-number")
        with pytest.raises(ValueError):
            get_settings()
