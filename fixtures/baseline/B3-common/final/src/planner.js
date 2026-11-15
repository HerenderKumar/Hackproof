function haversine(a, b) {
  const rad = (deg) => (deg * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

function plan(start, stops) {
  const left = stops.slice();
  const order = [];
  let here = start;
  let distance = 0;
  while (left.length > 0) {
    let best = 0;
    for (let i = 1; i < left.length; i++) {
      if (haversine(here, left[i]) < haversine(here, left[best])) best = i;
    }
    const [next] = left.splice(best, 1);
    distance += haversine(here, next);
    order.push(next.name);
    here = next;
  }
  return { order, distance };
}

module.exports = { plan, haversine };
