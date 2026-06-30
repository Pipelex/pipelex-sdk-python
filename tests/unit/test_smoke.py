import pipelex_sdk


class TestPackageImport:
    def test_package_name(self):
        assert pipelex_sdk.__name__ == "pipelex_sdk"
