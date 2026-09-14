from __future__ import annotations

from datetime import datetime, timezone


FETCHED_AT = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)


SEARCH_HTML = """
<!doctype html>
<html>
  <body>
    <ul id="restaurantList">
      <li>
        <a class="search__result__tour__item"
           href="/svc/contents/contentsView.do?menuSn=352&amp;vcontsId=12345">
          <div class="conts">
            <div class="sup"><p>Food</p></div>
            <div class="title"><p>Fixture Kitchen</p></div>
          </div>
        </a>
      </li>
      <li>
        <a class="search__result__tour__item"
           href="/svc/contents/contentsView.do?menuSn=352&amp;vcontsId=54321">
          <div class="conts">
            <div class="sup"><p>Food</p></div>
            <div class="title"><p>Fixture Cafe</p></div>
          </div>
        </a>
      </li>
    </ul>
    <ul id="attractionsList">
      <li>
        <a class="search__result__tour__item"
           href="/svc/contents/contentsView.do?menuSn=351&amp;vcontsId=67890">
          <div class="conts">
            <div class="sup"><p>History</p></div>
            <div class="title"><p>Fixture Palace</p></div>
          </div>
        </a>
      </li>
    </ul>
  </body>
</html>
"""


EMPTY_SEARCH_HTML = """
<!doctype html>
<html><body><ul id="restaurantList"></ul><ul id="attractionsList"></ul></body></html>
"""


DETAIL_HTML = """
<!doctype html>
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "TouristDestination",
        "name": "Fixture Kitchen",
        "address": {
          "@type": "PostalAddress",
          "streetAddress": "1 Sample-ro, Jung-gu, Seoul",
          "addressLocality": "Jung-gu",
          "addressRegion": "Seoul",
          "postalCode": "04500",
          "addressCountry": "KR"
        },
        "geo": {"latitude": "37.5665", "longitude": "126.9780"},
        "aggregateRating": {
          "ratingValue": "4.7",
          "bestRating": "5",
          "reviewCount": "1234"
        },
        "touristType": ["Food", "Korean cuisine"],
        "openingHoursSpecification": "11:00-21:00"
      }
    </script>
  </head>
  <body>
    <h1>Fixture Kitchen</h1>
    <div class="info_text"><strong>Address</strong><p>1 Sample-ro, Jung-gu, Seoul</p></div>
  </body>
</html>
"""


ATTRACTION_DETAIL_HTML = DETAIL_HTML.replace(
    "Fixture Kitchen", "Fixture Palace"
).replace("Food", "History")


def record_payload(
    *,
    source_id: str = "12345",
    category: str = "restaurant",
    name: str = "Fixture Kitchen",
    fetched_at: datetime = FETCHED_AT,
) -> dict[str, object]:
    return {
        "source": "visitkorea",
        "source_id": source_id,
        "source_url": f"https://english.visitkorea.or.kr/detail/{source_id}",
        "name": name,
        "category": category,
        "lat": 37.5665,
        "lng": 126.978,
        "address": "1 Sample-ro, Jung-gu, Seoul",
        "rating": None,
        "rating_scale": None,
        "review_count": None,
        "fetched_at": fetched_at,
        "subcategory": None,
        "raw_category": "Food" if category == "restaurant" else "History",
        "opening_information": "11:00-21:00",
        "tags": [],
    }
