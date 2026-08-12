from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from src.config import (
    ConfigError,
    environment_value_from,
    load_config,
    load_local_environment,
    validate_config,
)


class ConfigEnvironmentTests(unittest.TestCase):
    def test_dotenv_loads_missing_secret(self):
        key = "GAZETTE_TEST_DOTENV_KEY"
        original = os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                env_path = Path(directory) / ".env"
                env_path.write_text(f"{key}=from-dotenv\n", encoding="utf-8")
                load_local_environment(env_path)
                self.assertEqual(os.getenv(key), "from-dotenv")
        finally:
            os.environ.pop(key, None)
            if original is not None:
                os.environ[key] = original

    def test_topics_are_configurable_without_python_constants(self):
        config = load_config()
        custom = deepcopy(config)
        custom["topics"] = {
            "custom": {
                "name": "自定义主题",
                "name_en": "Custom Topic",
                "categories": [],
                "keywords": ["custom keyword"],
            }
        }
        custom["topic_rotation"] = {day: "custom" for day in custom["topic_rotation"]}
        validate_config(custom)

    def test_output_paths_cannot_escape_project(self):
        config = load_config()
        config["render"]["output_dir"] = "../private"
        with self.assertRaises(ConfigError):
            validate_config(config)

    def test_public_stylesheet_cannot_escape_project(self):
        config = load_config()
        config["render"]["public_stylesheet"] = "../../secret.css"
        with self.assertRaises(ConfigError):
            validate_config(config)

    def test_dotenv_does_not_override_process_environment(self):
        key = "GAZETTE_TEST_ENV_PRIORITY"
        original = os.environ.get(key)
        os.environ[key] = "from-process"
        try:
            with tempfile.TemporaryDirectory() as directory:
                env_path = Path(directory) / ".env"
                env_path.write_text(f"{key}=from-dotenv\n", encoding="utf-8")
                load_local_environment(env_path)
                self.assertEqual(os.getenv(key), "from-process")
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def test_llm_runtime_value_comes_from_configured_environment_variable(self):
        section = {
            "model_env": "GAZETTE_TEST_MODEL",
            "model": "fallback-model",
        }
        with patch.dict(os.environ, {"GAZETTE_TEST_MODEL": "runtime-model"}):
            self.assertEqual(environment_value_from(section, "model"), "runtime-model")

    def test_default_llm_config_contains_only_generic_environment_references(self):
        config = load_config()
        self.assertEqual(config["llm"]["coarse"]["api_key_env"], "COARSE_LLM_API_KEY")
        self.assertEqual(
            config["llm"]["editorial"]["api_key_env"],
            "EDITORIAL_LLM_API_KEY",
        )
        for section in config["llm"].values():
            self.assertNotIn("base_url", section)
            self.assertNotIn("model", section)
            self.assertNotIn("provider", section)

    def test_llm_environment_references_must_be_uppercase_names(self):
        config = load_config()
        config["llm"]["coarse"]["model_env"] = "invalid-model-name"
        with self.assertRaisesRegex(ConfigError, "llm.coarse.model_env"):
            validate_config(config)

    def test_well_known_quota_must_stay_between_one_and_three(self):
        config = load_config()
        config["selection"]["max_well_known_papers"] = 4
        with self.assertRaisesRegex(ConfigError, "max_well_known_papers"):
            validate_config(config)

    def test_well_known_minimum_cannot_exceed_edition_capacity(self):
        config = load_config()
        config["selection"]["min_well_known_papers"] = 3
        config["editorial_policy"]["min_selected_papers"] = 2
        config["editorial_policy"]["max_selected_papers"] = 2
        with self.assertRaisesRegex(ConfigError, "exceeds capacity"):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
