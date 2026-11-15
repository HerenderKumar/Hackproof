function splitBill(total, people, tipPercent) {
  const withTip = total * (1 + tipPercent / 100);
  const cents = Math.round(withTip * 100);
  const base = Math.floor(cents / people);
  const remainder = cents - base * people;
  const shares = [];
  for (let i = 0; i < people; i++) {
    shares.push((base + (i < remainder ? 1 : 0)) / 100);
  }
  return shares;
}

function average(values) {
  if (values.length === 0) return 0;
  let sum = 0;
  for (const value of values) sum += value;
  return sum / values.length;
}

module.exports = { splitBill, average };
