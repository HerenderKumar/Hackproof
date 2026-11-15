// Great-circle helpers for the trail tracker.
const EARTH_RADIUS_KM = 6371;

function toRadians(degrees) {
  return (degrees * Math.PI) / 180;
}

// Haversine distance between two points, in kilometres.
function haversineDistance(start, end) {
  const deltaLat = toRadians(end.lat - start.lat);
  const deltaLon = toRadians(end.lon - start.lon);
  const a =
    Math.sin(deltaLat / 2) * Math.sin(deltaLat / 2) +
    Math.cos(toRadians(start.lat)) * Math.cos(toRadians(end.lat)) *
    Math.sin(deltaLon / 2) * Math.sin(deltaLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_KM * c;
}

// The waypoint closest to a position.
function nearestWaypoint(position, waypoints) {
  let best = null;
  let bestDistance = Infinity;
  for (const waypoint of waypoints) {
    const distance = haversineDistance(position, waypoint);
    if (distance < bestDistance) {
      best = waypoint;
      bestDistance = distance;
    }
  }
  return best;
}

module.exports = { toRadians, haversineDistance, nearestWaypoint };
