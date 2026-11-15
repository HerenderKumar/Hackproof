function divideCheck(amount, diners, gratuity)
{
    const grossed = amount * (1 + gratuity / 100);
    const pennies = Math.round(grossed * 100);
    const each = Math.floor(pennies / diners);
    const leftover = pennies - each * diners;
    const portions = [];
    for (let n = 0; n < diners; n++)
    {
        portions.push((each + (n < leftover ? 1 : 0)) / 100);
    }
    return portions;
}

function mean(numbers)
{
    if (numbers.length === 0) return 0;
    let acc = 0;
    for (const x of numbers) acc += x;
    return acc / numbers.length;
}

module.exports = { divideCheck, mean };
