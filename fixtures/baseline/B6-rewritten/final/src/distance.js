/*
 * Distances on the globe. Used by the route planner to pick the next stop.
 */
export const EARTH_RADIUS_KM = 6371.0;

export function toRadians(angle) {
  const half = 180;
  return angle * (Math.PI / half);
}

export function haversineDistance(from, to) {
  const [lat1, lat2] = [toRadians(from.lat), toRadians(to.lat)];
  const sinLat = Math.sin(toRadians(to.lat - from.lat) / 2);
  const sinLon = Math.sin(toRadians(to.lon - from.lon) / 2);
  const h = sinLat ** 2 + Math.cos(lat1) * Math.cos(lat2) * sinLon ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function nearestWaypoint(here, stops) {
  return stops.reduce(
    (closest, stop) =>
      closest === undefined ||
      haversineDistance(here, stop) < haversineDistance(here, closest)
        ? stop
        : closest,
    undefined,
  );
}
