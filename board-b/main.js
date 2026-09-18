/**
 * VegTrack — Board B (Receiver / "Shop Unit")
 * วางที่ร้าน/โต๊ะสาธิต — ไม่โดนกระแทก
 * รับข้อมูลจาก Board A ทาง Radio แล้วส่งต่อออก USB Serial เข้าคอมพิวเตอร์
 * (คอมพิวเตอร์รัน server/vegtrack_dashboard.py หรือ server/vegtrack_logger.py ต่อ)
 *
 * Hardware: ไม่มีอุปกรณ์ต่อพ่วง ใช้จอ LED 5x5 ในตัวโชว์เกรดตัวเดียวพอ
 * Paste into makecode.microbit.org -> JavaScript tab
 */

radio.setGroup(11)   // ต้องตรงกับ Board A เป๊ะ

let last_grade = "-"
let rT = 0
let rH = 0
let rW = 0
let rD = 0
let rI = 0

// รับค่าตัวเลขที่ Board A ส่งมาเป็นแพ็กเก็ตแยก
radio.onReceivedValue(function (name: string, value: number) {
    if (name == "T") rT = value
    else if (name == "H") rH = value
    else if (name == "W") rW = value
    else if (name == "D") rD = value
    else if (name == "I") rI = value
})

// รับ "เกรด|เหตุการณ์" แล้วประกอบกลับเป็นบรรทัดเต็มส่งออก Serial
radio.onReceivedString(function (receivedString) {
    let parts = receivedString.split("|")
    let grade = parts[0]
    let event = parts.length > 1 ? parts[1] : "-"
    last_grade = grade

    let line = "T=" + rT + " H=" + rH + " W=" + rW +
        " Days=" + rD + " Grade=" + grade +
        " Impact=" + rI + " Event=" + event
    serial.writeLine(line)
    basic.showString(grade)
})

input.onButtonPressed(Button.A, function () {
    basic.showString(last_grade)
})

basic.forever(function () {
    basic.pause(1000)
})
