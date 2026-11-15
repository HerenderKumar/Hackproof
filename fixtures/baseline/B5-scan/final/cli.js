const { forecast, warmest } = require("./weather");

async function main(argv) {
  const city = argv[2];
  if (!city) {
    console.error("usage: node cli.js <city>");
    return 2;
  }
  const days = await forecast(city);
  const best = warmest(days);
  console.log(`${city}: warmest on ${best.date} at ${best.high} C`);
  return 0;
}

main(process.argv).then((code) => process.exit(code));
