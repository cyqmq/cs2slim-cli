__version__ = "1.0.2"


def main():
    from .cli import main as _main

    return _main()


__all__ = ["main", "__version__"]
