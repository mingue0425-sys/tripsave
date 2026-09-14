BOOKING_SEARCH_HTML = """
<!doctype html>
<html><body>
  <div data-testid="property-card">
    <h3><a data-testid="title-link" href="https://www.booking.com/hotel/kr/sample-hotel.html?checkin=2026-10-01&amp;checkout=2026-10-02&amp;matching_block_id=block-1">
      <div data-testid="title">Sample Hotel</div>
    </a></h3>
    <a href="https://www.booking.com/hotel/kr/sample-hotel.html">
      <span data-testid="address-link">Jung-Gu, Seoul</span>
      <span>Show on map</span>
    </a>
    <button data-testid="distance">1.2 km from downtown</button>
    <a data-testid="review-score-link">
      <div data-testid="review-score">Scored 8.7 8.7 Wonderful 467 reviews</div>
    </a>
    <div data-testid="recommended-units"><h4>Deluxe Room</h4></div>
    <div data-testid="availability-rate-information">
      <div data-testid="price-for-x-nights">1 night, 2 adults</div>
      <span data-testid="price-and-discounted-price">KRW&nbsp;100,000</span>
      <div data-testid="taxes-and-charges">Includes taxes and fees</div>
    </div>
  </div>
  <div data-testid="property-card">
    <h3><a data-testid="title-link" href="https://www.booking.com/hotel/kr/explicit-tax-hotel.html?checkin=2026-10-01&amp;checkout=2026-10-02&amp;matching_block_id=block-2">
      <div data-testid="title">Explicit Tax Hotel</div>
    </a></h3>
    <a href="https://www.booking.com/hotel/kr/explicit-tax-hotel.html">
      <span data-testid="address-link">Haeundae, Busan</span>
      <span>Show on map</span>
    </a>
    <button data-testid="distance">3 km from downtown</button>
    <a data-testid="review-score-link">
      <div data-testid="review-score">Scored 9.2 9.2 Superb 1,234 reviews</div>
    </a>
    <div data-testid="recommended-units"><h4>Ocean View Twin Room</h4></div>
    <div data-testid="availability-rate-information">
      <span data-testid="price-and-discounted-price">KRW 200,000</span>
      <div data-testid="taxes-and-charges">+KRW 20,000 taxes and fees</div>
    </div>
  </div>
</body></html>
"""
