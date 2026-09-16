from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.weather.models import WeatherStatus
from backend.weather.parser import (
    KmaWebLocationMismatchError,
    KmaWebPageChangedError,
    parse_kma_web_html,
    parse_precipitation_text,
    parse_probability_text,
    parse_temperature_text,
    parse_wind_speed_text,
)


FIXTURE = Path(__file__).with_name("fixtures") / "kma_web_forecast.html"
SOURCE_URL = "https://www.weather.go.kr/w/wnuri-fct2021/main/digital-forecast.do"
FETCHED_AT = datetime(2026, 9, 16, 1, tzinfo=timezone.utc)


def parse_fixture():
    return parse_kma_web_html(
        FIXTURE.read_text(encoding="utf-8"),
        source_url=SOURCE_URL,
        fetched_at=FETCHED_AT,
    )


def test_parser_keeps_visible_fields_and_unknowns_distinct():
    result = parse_fixture()

    sunny, rain, snow, partial = result.days
    assert sunny.condition == "맑음"
    assert sunny.raw_condition == "맑음"
    assert sunny.temp_min_c == 14
    assert sunny.temp_max_c == 29
    assert sunny.precipitation_probability_pct == 0
    assert sunny.precipitation_mm is None
    assert sunny.wind_speed_mps == 3
    assert sunny.status is WeatherStatus.PARTIAL
    assert "precipitation" in sunny.missing_fields

    assert rain.condition == "구름많음/비"
    assert rain.raw_condition == "구름많음/가끔 비"
    assert rain.precipitation_mm is None
    assert rain.precipitation_min_mm == 5
    assert rain.precipitation_max_mm == 10
    assert rain.precipitation_text == "5~10mm"
    assert rain.wind_speed_mps == pytest.approx(1.0)

    assert snow.condition == "눈"
    assert snow.temp_min_c == -3
    assert snow.temp_max_c == 4
    assert snow.precipitation_probability_pct == 70
    assert snow.precipitation_mm is None
    assert snow.precipitation_max_mm == 1
    assert snow.precipitation_text == "1mm 미만"
    assert snow.wind_speed_mps is None

    assert partial.condition is None
    assert partial.temp_min_c == 1
    assert partial.temp_max_c == 8
    assert partial.status is WeatherStatus.PARTIAL


def test_scalar_parsers_range_check_units_and_zero():
    assert parse_temperature_text("-3°") == -3
    assert parse_temperature_text("12℃") == 12
    assert parse_temperature_text("12°F") is None
    assert parse_probability_text("70 %") == 70
    assert parse_probability_text("170%") is None
    assert parse_probability_text("-") is None
    assert parse_wind_speed_text("3m/s") == 3
    assert parse_wind_speed_text("3.6 km/h") == pytest.approx(1.0)
    assert parse_wind_speed_text("약") is None
    assert parse_precipitation_text("0mm").mm == 0
    assert parse_precipitation_text("1mm 미만").mm is None
    assert parse_precipitation_text("1mm 미만").max_mm == 1
    assert parse_precipitation_text("5~10mm").min_mm == 5
    assert parse_precipitation_text("5~10mm").max_mm == 10


def test_partial_precipitation_never_exposes_a_partial_total():
    html = """
    <section id="digital-forecast">
      <a href="?dongCode=1114055000">서울</a>
      <article class="dfs-daily-slide" data-date="2026-09-20">
        <div class="daily-weather-am"><span class="wic">맑음</span></div>
        <div class="daily-minmax"><strong>최저</strong><span>10℃</span><strong>최고</strong><span>20℃</span></div>
      </article>
      <div class="daily" data-date="2026-09-20">
        <ul class="item" data-date="2026-09-20">
          <li class="pcp"><span>5mm</span></li>
          <li class="pcp"><span>자료없음</span></li>
        </ul>
      </div>
    </section>
    """
    day = parse_kma_web_html(
        html,
        source_url=SOURCE_URL,
        fetched_at=FETCHED_AT,
    ).days[0]

    assert day.precipitation_mm is None
    assert day.precipitation_min_mm is None
    assert day.precipitation_max_mm is None
    assert "precipitation" in day.missing_fields
    assert day.precipitation_text == "5mm / 자료없음"


def test_parser_rejects_temperature_inversion_as_page_changed():
    html = """
    <section id="digital-forecast">
      <a href="?dongCode=1114055000">서울</a>
      <article class="dfs-daily-slide" data-date="2026-09-20">
        <div class="daily-weather-am"><span class="wic">맑음</span></div>
        <div class="daily-minmax"><strong>최저</strong><span>22℃</span><strong>최고</strong><span>10℃</span></div>
      </article>
    </section>
    """
    with pytest.raises(KmaWebPageChangedError):
        parse_kma_web_html(html, source_url=SOURCE_URL, fetched_at=FETCHED_AT)


def test_parser_accepts_semantic_layout_variation():
    html = """
    <section class="cmp-dfs-slider">
      <article class="dfs-daily-slide" data-date="2026-09-20">
        <div class="daily-weather-am"><span class="wic">흐림</span></div>
        <table class="daily-minmax"><tr><th>최저기온</th><td>-1℃</td></tr><tr><th>최고기온</th><td>7℃</td></tr></table>
        <p class="daily-pop-am"><span>강수확률</span><strong>30%</strong></p>
      </article>
      <div class="daily" data-date="2026-09-20">
        <ul class="item" data-date="2026-09-20">
          <li><span class="hid">바람: </span><span class="wspd">2 m/s</span></li>
        </ul>
      </div>
    </section>
    """
    day = parse_kma_web_html(html, source_url=SOURCE_URL, fetched_at=FETCHED_AT).days[0]
    assert day.condition == "흐림"
    assert day.temp_min_c == -1
    assert day.temp_max_c == 7
    assert day.precipitation_probability_pct == 30
    assert day.wind_speed_mps == 2


def test_parser_rejects_missing_critical_structure_and_wrong_area():
    with pytest.raises(KmaWebPageChangedError):
        parse_kma_web_html("<main>no forecast</main>", source_url=SOURCE_URL, fetched_at=FETCHED_AT)
    with pytest.raises(KmaWebLocationMismatchError):
        parse_kma_web_html(
            FIXTURE.read_text(encoding="utf-8"),
            source_url=SOURCE_URL,
            fetched_at=FETCHED_AT,
            expected_location_code="9999999999",
        )
