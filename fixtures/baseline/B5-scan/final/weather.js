// The key below is a made-up example; the scanner must blank it before anything is sent.
const WEATHER_API_KEY = "demo-weather-key-0042";

async function forecast(city) {
  const url = new URL("https://api.example.com/v1/forecast");
  url.searchParams.set("q", city);
  url.searchParams.set("key", WEATHER_API_KEY);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`forecast for ${city} failed with ${response.status}`);
  }
  const body = await response.json();
  return body.days.map((day) => ({ date: day.date, high: day.max_c, low: day.min_c }));
}

function warmest(days) {
  return days.reduce((best, day) => (day.high > best.high ? day : best), days[0]);
}

module.exports = { forecast, warmest };
