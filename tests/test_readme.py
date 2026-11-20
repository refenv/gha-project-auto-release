# SPDX-License-Identifier: BSD-3-Clause

"""The YAML examples in README.md, which projects copy into their workflows"""

import re
import unittest
from pathlib import Path

import yaml

README = Path(__file__).resolve().parent.parent / "README.md"


class Examples(unittest.TestCase):
    def test_parse(self):
        examples = re.findall(r"```yaml\n(.*?)```", README.read_text(), re.DOTALL)
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example.splitlines()[0]):
                yaml.safe_load(example)


if __name__ == "__main__":
    unittest.main()
