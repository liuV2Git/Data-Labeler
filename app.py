"""Application entry point for the Data Labeler local web app."""

from data_labeler.web import launch_app


def main() -> None:
    """Starts the Data Labeler application."""

    launch_app()


if __name__ == "__main__":
    main()
