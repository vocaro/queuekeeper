"""Core composes only the wrappers registered by enabled extensions."""
import unittest
from unittest.mock import patch
import host_isolation
from queuekeeper import extensions, shims


class Environment(unittest.TestCase):
    def test_core_keeps_a_command_environment_without_platform_wrappers(self):
        base = {"PATH": "/usr/bin:/bin", "OTHER": "1"}
        with patch.object(extensions, "COMMAND_WRAPPERS", []):
            self.assertEqual(shims.environment(base, "/tmp"), base)
            self.assertIsNot(shims.environment(base, "/tmp"), base)

    def test_enabled_wrappers_compose_in_registration_order(self):
        def first(base, root): return {**base, "PATH": "first:" + base["PATH"], "root": root}
        def second(base, root): return {**base, "PATH": "second:" + base["PATH"]}
        with patch.object(extensions, "COMMAND_WRAPPERS", [first, second]):
            self.assertEqual(shims.environment({"PATH": "tool"}, "/project"),
                             {"PATH": "second:first:tool", "root": "/project"})
