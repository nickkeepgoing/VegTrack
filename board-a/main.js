/**
 * VegTrack — Board A (Sender / "Crate Unit")
 * ติดในลังผัก — อ่าน DHT22, จำแนกเหตุการณ์ด้วย Edge AI,
 * คำนวณเกรด A/B/C จากสมการที่สอบเทียบจากการทดลองจริง, ส่งออกทาง Radio
 *
 * Hardware:
 *   DHT22 DATA -> P1   (อย่าใช้ P0 — เคยพบว่าขา P0 เสียบนบอร์ดที่ใช้ทดสอบ)
 *   DHT22 VCC  -> 3V
 *   DHT22 GND  -> GND
 *
 * Paste into makecode.microbit.org -> JavaScript tab (ไม่ใช่ Python)
 * ต้องเพิ่ม extension: DHT11_DHT22 (alankrantas/pxt-DHT11_DHT22)
 * และโมเดล ML จาก micro:bit CreateAI (5 classes: Still, SmoothRoad, RoughRoad, Lift, Impact)
 */

radio.setGroup(11)   // ต้องตรงกับ Board B เป๊ะ

let impact_total = 0
let temp = 0
let humidity = 0
let pct_weight = 100.0
let last_event = "still"

// ค่าคงที่ที่สอบเทียบจากการทดลองจริง (ดู /docs/equation.md)
const SLOPE = 0.1685
const INTERCEPT = 4.2172
const SPOIL_PCT = 80.0
const MIN_RATE = 2.0
const GRADE_A_DAYS = 1.5
const GRADE_B_DAYS = 0.5

// ---------- Edge AI event handlers ----------
ml.onStart(ml.event.Still, function () {
    last_event = "still"
})

ml.onStart(ml.event.SmoothRoad, function () {
    last_event = "smooth"
})

ml.onStart(ml.event.RoughRoad, function () {
    last_event = "rough"
})

ml.onStart(ml.event.Lift, function () {
    last_event = "lift"
})

ml.onStart(ml.event.Impact, function () {
    last_event = "impact"
    impact_total += 1               // ดัชนีความบอบช้ำ — ไม่เข้าสมการ แสดงแยกต่างหาก
    basic.showIcon(IconNames.Ghost)
    basic.pause(150)
    basic.clearScreen()
})

// ---------- Sensor ----------
function readSensor() {
    dht11_dht22.queryData(DHTtype.DHT22, DigitalPin.P1, true, false, true)
    let t = dht11_dht22.readData(dataType.temperature)
    let h = dht11_dht22.readData(dataType.humidity)
    // กรองค่าที่อ่านผิด (-999 หรือค่านอกช่วงที่เป็นไปได้จริง)
    // ถ้าค่าใหม่ใช้ไม่ได้ จะคงค่าล่าสุดที่ถูกต้องไว้แทน ไม่ค้าง ไม่โชว์ค่าขยะ
    if (t != -999 && h >= 0 && h <= 100 && t >= 15 && t <= 45) {
        temp = t
        humidity = h
    }
}

// ---------- Equation (อุณหภูมิเท่านั้น — ไม่รวม impact) ----------
function weightLossRate(t: number) {
    let rate = SLOPE * t + INTERCEPT
    if (rate < MIN_RATE) rate = MIN_RATE
    return rate
}

function daysRemaining(pct: number, t: number) {
    let d = (pct - SPOIL_PCT) / weightLossRate(t)
    if (d < 0) d = 0
    return d
}

function gradeFromDays(d: number) {
    if (d >= GRADE_A_DAYS) return "A"
    if (d >= GRADE_B_DAYS) return "B"
    return "C"
}

// ---------- Main loop ----------
basic.forever(function () {
    readSensor()
    let rate = weightLossRate(temp)
    pct_weight = pct_weight - rate * (4.0 / (24.0 * 60.0 * 60.0))
    if (pct_weight < 0) pct_weight = 0
    let days = daysRemaining(pct_weight, temp)
    let grade = gradeFromDays(days)
    let w_round = Math.round(pct_weight * 10) / 10
    let d_round = Math.round(days * 100) / 100

    // สำหรับ debug ตอนเสียบ USB ตรง
    let line = "T=" + temp + " H=" + humidity + " W=" + w_round +
        " Days=" + d_round + " Grade=" + grade +
        " Impact=" + impact_total + " Event=" + last_event
    serial.writeLine(line)

    // ส่งผ่าน Radio แบบแยกแพ็กเก็ตเล็ก (กันข้อความถูกตัดที่ ~19-20 ตัวอักษร)
    radio.sendValue("T", temp)
    radio.sendValue("H", humidity)
    radio.sendValue("W", w_round)
    radio.sendValue("D", d_round)
    radio.sendValue("I", impact_total)
    radio.sendString(grade + "|" + last_event)

    basic.showString(grade)
    basic.pause(4000)
})

input.onButtonPressed(Button.A, function () {
    basic.showNumber(temp)
})

input.onButtonPressed(Button.B, function () {
    basic.showNumber(impact_total)
})
