from app.main import html_date_value


def test_html_date_value_normalizes_imported_date_formats() -> None:
    assert html_date_value("20220101") == "2022-01-01"
    assert html_date_value("2022-01-01 00:00:00") == "2022-01-01"
    assert html_date_value("") == ""
