from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.config import ConfigError, load_config, load_local_environment, validate_config


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


if __name__ == "__main__":
    unittest.main()
