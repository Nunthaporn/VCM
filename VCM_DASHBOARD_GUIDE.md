# คู่มือ Production Process Monitoring

เอกสารนี้อธิบายที่มาของข้อมูล วิธีใช้ตัวกรอง ความหมายของ KPI และวิธีอ่านข้อมูลในแต่ละแท็บของแดชบอร์ด `VCM.py`

## 1. วัตถุประสงค์ของแดชบอร์ด

แดชบอร์ดใช้ติดตามการเคลื่อนที่ของงานตามลำดับกระบวนการ:

`CUT → SM2 → SM3 → SEW → WAIT_FN → PACK`

วัตถุประสงค์หลักคือ:

- ดูจำนวน Barcode ทั้งหมด งานที่จบแล้ว และงานที่ยังไม่จบ
- ดูว่าแต่ละ Barcode ผ่าน Process ใดแล้วและปัจจุบันอยู่ที่ใด
- ดูจำนวนชิ้นงาน (`Qty`) และงานระหว่างทำ (`WIP`) ของแต่ละ Process
- วิเคราะห์ระยะเวลาระหว่าง Process ด้วย Median และ Mode หน่วยเป็นนาที
- ระบุช่วงที่มีเวลานาน งานรอมาก หรือมีแนวโน้มเป็น Bottleneck
- เจาะรายละเอียดระดับ SO และ Barcode

## 2. แหล่งข้อมูลและการจับคู่คอลัมน์

ข้อมูลมาจาก PostgreSQL โดยกำหนด Schema ผ่าน `VCM_SOURCE_SCHEMA` และเชื่อมต่อด้วยค่าจากไฟล์ `.env`

| Process | View | วันและเวลา Process | Input Qty | Output Qty |
|---|---|---|---|---|
| CUT | `v_cut` | `cut_date` หรือ `upd_date` ตามเงื่อนไข CUT | `cut_qty` | `cut_qty - defect_qty` |
| SM2 | `v_sm2` | `ww_in_date` | `ww_in_qty` | `ww_in_qty - waste_qty` |
| SM3 | `v_sm3` | `ww_in_date` | `ww_in_qty` | `ww_in_qty - waste_qty` |
| SEW | `v_sew` | `loading_date` | `qty` | `wait_fn_qty` |
| WAIT_FN | `v_sew` | `wait_fn_date` | `wait_fn_qty` | `wait_fn_qty - defect_qty` |
| PACK | `v_pack` | `entry_date` | `qty` | `qty` |

ค่า `defect_qty` และ `waste_qty` ที่เป็น `NULL` หรือแปลงเป็นตัวเลขไม่ได้จะถูกแทนด้วย `0` ก่อนคำนวณ Output

### การรวมข้อมูล

ข้อมูลถูกจัดกลุ่มด้วย:

`plant + so_year + so_no + barcode + process`

ภายในกลุ่มเดียวกัน:

- `process_date` ใช้วันและเวลาที่เก่าที่สุด หรือ First Scan
- `input_qty`, `output_qty` และ `defect_qty` ใช้ผลรวมของรายการทั้งหมดในกลุ่ม
- ค่าปริมาณที่แปลงเป็นตัวเลขไม่ได้หรือเป็น `NULL` จะถูกแทนด้วย `0`

ระบบอ่านข้อมูลครั้งละ 200,000 แถว และ Cache ข้อมูลไว้ 1 ชั่วโมง ค่า `Data refreshed` คือเวลาที่ Cache ชุดปัจจุบันถูกสร้าง

## 3. กฎวันที่และเวลา

### วันที่เริ่มต้นของ SM2 และ WAIT_FN

- SM2 ใช้เฉพาะรายการที่ `ww_in_date >= 2026-07-09`
- WAIT_FN ใช้เฉพาะรายการที่ `wait_fn_date >= 2026-07-09`
- วันที่ `2026-07-09` ถูกรวมในการคำนวณด้วย

### การเลือกเวลาของ CUT

1. ถ้า `cut_date` มีเวลาและไม่ใช่ `00:00:00` ให้ใช้ `cut_date`
2. ถ้า `cut_date` ว่างหรือเวลาเป็น `00:00:00` และ `upd_date` มีค่า ให้ใช้ `upd_date`
3. ถ้า `cut_date` เป็น `00:00:00` แต่ `upd_date` ว่าง ให้เก็บ `cut_date` เวลา `00:00:00` ไว้

CUT เวลา `00:00:00` ยังคงใช้แสดงว่ามี CUT Scan และใช้ใน Timeline แต่ไม่ถูกนำไปคำนวณ:

- Median และ Mode ของช่วง `CUT → SM2`
- Median Aging ของงานรอที่ CUT
- ค่าเฉลี่ย Aging เมื่อ Process ปัจจุบันคือ CUT

ค่าเวลาติดลบ เช่น เวลาปลายทางน้อยกว่าเวลาต้นทาง จะไม่ถูกนำไปคำนวณ Median, Mode และ Outlier

## 4. ตัวกรองด้านบน

ตัวกรองทั้งสามช่องมีผลกับ KPI, Production Flow, Time & Waiting Analysis และ Barcode Drill-down

### Factory

- `ALL` แสดงข้อมูลทุกโรงงาน
- ตัวเลือกโรงงานประกอบด้วย `G1`, `G2`, `G3`, `G4`, `TRM` และ `EA`
- เมื่อเปลี่ยน Factory ระบบจะล้าง SO NO และ Barcode กลับเป็น `All`

### SO NO

- แสดงเฉพาะ SO ที่อยู่ใน Factory ที่เลือก
- สามารถพิมพ์ค่า SO NO ได้
- เมื่อเปลี่ยน SO NO ระบบจะล้าง Barcode กลับเป็น `All`

### Barcode

- แสดงเฉพาะ Barcode ที่อยู่ใน Factory และ SO NO ที่เลือก
- ใช้ Barcode ช่องนี้ร่วมกับแท็บ Barcode Drill-down โดยไม่มีช่องเลือก Barcode ซ้ำด้านล่าง

## 5. KPI ด้านบน

KPI ทั้งหมดเปลี่ยนตามตัวกรองทันที

### Total Barcodes

จำนวนรายการ Barcode ใน Summary หลังใช้ตัวกรอง หนึ่งรายการถูกระบุด้วยชุดข้อมูล `barcode + so_year + so_no`

### Completed Barcodes

จำนวน Barcode ที่ Process ปัจจุบันเป็น `PACK` และมีสถานะ `COMPLETED`

### Open Barcodes

คำนวณจาก:

`Total Barcodes - Completed Barcodes`

จึงรวมทั้งสถานะ `WAITING` และ `IN PROCESS`

### Completion Rate

คำนวณจาก:

`Completed Barcodes ÷ Total Barcodes × 100`

### Avg. Aging (Minutes)

ค่าเฉลี่ยจำนวนนาทีตั้งแต่ First Scan ของ Process ปัจจุบันจนถึงเวลาปัจจุบัน โดยใช้เฉพาะ Barcode ที่ยังไม่ `COMPLETED`

กรณี Barcode ปัจจุบันอยู่ CUT และ CUT time เป็น `00:00:00` รายการนั้นจะไม่มี Aging และไม่ถูกนำมาหาค่าเฉลี่ย

## 6. แท็บ Production Flow

แท็บนี้สรุปภาพรวมการไหลของงานจาก CUT ถึง PACK

### การ์ด Process

แต่ละ Process แสดง:

- `Input`: ผลรวมปริมาณที่เข้าสู่ Process ตามตัวกรอง
- `Output`: ผลรวมปริมาณที่ผ่านออกจาก Process ตามสูตรของแต่ละ Process
- `WIP (pcs)`: ผลรวม Input Qty ของ Barcode ที่พบ Process ต้นทางแล้ว แต่ยังไม่พบ Process ถัดไป
- เมื่อนำเมาส์ชี้บนการ์ด จะแสดงทั้ง WIP หน่วยชิ้นและจำนวน Barcode ที่เข้าเงื่อนไขเดียวกัน

### สูตร WIP

WIP ใช้เวลา Scan เป็นเงื่อนไข โดยแต่ละ Process ใช้ Quantity และจำนวน Barcode จากข้อมูลของ Process นั้นเอง ไม่ได้ใช้ CUT เป็นฐานร่วมกัน:

| WIP | เงื่อนไข | จำนวนชิ้นบนการ์ด | จำนวนใน Tooltip |
|---|---|---|---|
| CUT | มี CUT time และไม่มี SM2 time | จำนวนรายการ Barcode จาก CUT | ผลรวม `cut_qty` |
| SM2 | มี SM2 time และไม่มี SM3 time | จำนวนรายการ Barcode จาก SM2 | ผลรวม `ww_in_qty` ของ SM2 |
| SM3 | มี SM3 time และไม่มี SEW time | จำนวนรายการ Barcode จาก SM3 | ผลรวม `ww_in_qty` ของ SM3 |
| SEW | มี SEW time และไม่มี WAIT_FN time | จำนวนรายการ Barcode จาก SEW | ผลรวม `qty` ของ SEW |
| WAIT_FN | มี WAIT_FN time และไม่มี PACK time | จำนวนรายการ Barcode จาก WAIT_FN | ผลรวม `wait_fn_qty` |
| PACK | จบกระบวนการ | `0` | `0` |

ตัวอย่างแนวคิดของ CUT:

```sql
SUM(cut_qty) FILTER (
    WHERE cut_time IS NOT NULL AND sm2_time IS NULL
) AS cut_wip_pcs,
COUNT(DISTINCT barcode) FILTER (
    WHERE cut_time IS NOT NULL AND sm2_time IS NULL
) AS cut_wip_barcode_count
```

ข้อควรระวัง: เงื่อนไข WIP ตรวจเป็นรายคู่ Process หากข้อมูลข้ามขั้น เช่น มี SEW แต่ไม่มี SM3 ระบบยังถือว่า SM3 ยังไม่พบตามข้อมูลจริง การตรวจคุณภาพของ Scan จึงยังสำคัญ

### ตัวเชื่อมระหว่าง Process

ระหว่างการ์ดจะแสดงเวลาของแต่ละช่วงเป็นนาที:

- `Median`: ค่ากึ่งกลางของระยะเวลาทั้งหมด เหมาะกับข้อมูลที่มีค่ามากผิดปกติ
- `Mode`: ระยะเวลาที่พบซ้ำบ่อยที่สุด หลังปัดเป็นทศนิยมสองตำแหน่ง
- `-`: ไม่มีคู่เวลาต้นทางและปลายทางที่ใช้คำนวณได้

## 7. แท็บ Time & Waiting Analysis

แท็บนี้ใช้วิเคราะห์เวลา ปริมาณงานรอ และ Potential Bottleneck

### Operational Story

ส่วนนี้แปลงผลวิเคราะห์เป็นเรื่องราวและคำแนะนำที่อ่านได้ทันที โดยเปลี่ยนตาม Factory, SO NO และ Barcode ที่เลือก ประกอบด้วย:

- `สิ่งที่เกิดขึ้น`: เลือก Flow ที่มี WIP จำนวนชิ้นสูงสุด พร้อมแสดงจำนวน Barcode ที่เกี่ยวข้อง
- `ระดับความรุนแรง`: ระบุ Flow ที่มี Median Elapsed Time สูงที่สุด และเปรียบเทียบเป็นจำนวนเท่ากับ Median ของทุก Flow ที่มีข้อมูล
- `ควรตรวจสอบก่อน`: เลือก Barcode ที่ยังไม่ Completed และมี Aging สูงสุด พร้อมแสดง SO, Current Position และ Aging หน่วยนาที

หลักการจัดลำดับนี้ช่วยตอบคำถามต่อเนื่องว่า “เกิดอะไรขึ้น → รุนแรงเพียงใด → ควรเริ่มตรวจงานใด” แต่ยังควรตรวจความครบถ้วนของเวลา Scan และบริบทการผลิตก่อนดำเนินการ

### Key Finding

เลือก Process Flow ที่มี `Median Elapsed Time` สูงที่สุด พร้อมแสดง Median และ Mode หน่วยเป็นนาที

ควรใช้เป็นจุดเริ่มต้นในการตรวจสอบ ไม่ควรสรุปว่าเป็นปัญหาแน่นอนโดยไม่ดูจำนวนตัวอย่าง ปริมาณงาน และคุณภาพเวลา Scan เพิ่มเติม

### Abnormal Elapsed Time

แสดง Flow ที่มี Outlier เด่นที่สุด โดยเรียงจากจำนวน Outlier และ Median

เกณฑ์ Outlier ใช้วิธี IQR เมื่อมีตัวอย่างอย่างน้อย 4 รายการ:

`Outlier Threshold = Q3 + 1.5 × (Q3 - Q1)`

ค่าที่สูงกว่า Threshold จะถูกนับเป็น Outlier แต่ยังคงรวมอยู่ในการหา Median และ Mode

### Highest Waiting Volume

เลือก Flow ที่มีจำนวน Barcode รอมากที่สุด และแสดง:

- จำนวน Barcode ที่รอ
- WIP หน่วยชิ้น
- Median Waiting Time หน่วยนาที

### กราฟ Process Elapsed Time (Minutes)

กราฟแท่งเปรียบเทียบ Median และ Mode ของระยะเวลาระหว่าง Process:

`เวลาปลายทาง - เวลาต้นทาง`

ใช้เฉพาะรายการที่มีเวลาทั้งสองฝั่งและระยะเวลาไม่ติดลบ สำหรับ `CUT → SM2` จะไม่นำ CUT เวลา `00:00:00` มาคำนวณ

### กราฟ Waiting Barcode by Process Flow

แสดงจำนวน Barcode ที่มี Process ต้นทางแล้ว แต่ยังไม่มี Process ปลายทาง เช่น:

- `SM2 → SM3 = 60` หมายถึงมี 60 Barcode ที่พบ SM2 time แต่ยังไม่พบ SM3 time
- จำนวนนี้เป็นจำนวน Barcode ไม่ใช่จำนวนชิ้น

### กราฟ Potential Bottleneck by Process Flow

องค์ประกอบของกราฟ Bubble:

- แกน X: Median Aging ของงานรอ หน่วยนาที
- แกน Y: จำนวน Barcode ที่รอ
- ขนาด Bubble: WIP หน่วยชิ้น
- สี Bubble: Median Elapsed Time หน่วยนาที

จุดที่อยู่ขวาและสูง รวมถึงมี Bubble ขนาดใหญ่ ควรได้รับการตรวจสอบก่อน เพราะหมายถึงรอนาน มีหลาย Barcode และมีจำนวนชิ้นค้างมาก

### กราฟ Process Quantity by Flow (Pcs)

แสดงผลรวม `output_qty` ของแต่ละ Process ตามตัวกรอง หน่วยเป็นชิ้น ตัวเลขนี้เป็นปริมาณ Output ไม่ใช่ WIP และไม่ใช่จำนวน Barcode

## 8. แท็บ Barcode Drill-down

แท็บนี้ใช้ตรวจสอบ Barcode เดียว โดยเลือก Barcode จากตัวกรองด้านบน

หาก Barcode ยังเป็น `All` ระบบจะแสดงข้อความให้เลือก Barcode ก่อน

### Current Position

แสดง Process ล่าสุดที่พบตามลำดับ Process:

- ถ้ายังไม่ถึง PACK และมี Output Qty มากกว่า 0 จะแสดงรูปแบบ `Process ปัจจุบัน → Process ถัดไป`
- ตัวอย่าง `WAIT_FN → PACK` หมายถึงพบ WAIT_FN แล้วและกำลังรอ PACK
- ถ้าถึง PACK จะแสดง `PACK`

### Status

- `COMPLETED`: Process ล่าสุดคือ PACK
- `WAITING`: Process ล่าสุดยังไม่ใช่ PACK และ Output Qty ของ Process ปัจจุบันมากกว่า 0
- `IN PROCESS`: Process ล่าสุดยังไม่ใช่ PACK แต่ Output Qty ของ Process ปัจจุบันเป็น 0

ด้านล่าง Status จะแสดง SO NO ของ Barcode ที่เลือก

### Minutes Since CUT

จำนวนนาทีจาก CUT time ถึงเวลาปัจจุบัน:

`เวลาปัจจุบัน - CUT time`

ถ้าไม่พบ CUT time จะแสดง `-` ค่าในช่องนี้เป็นรายละเอียด Barcode โดยตรง จึงยังคำนวณจาก CUT เวลา `00:00:00` ที่ไม่มี `upd_date` ได้ แม้เวลานั้นจะไม่ถูกใช้ในค่าสถิติรวม

### Total Lead Time

จำนวนนาทีตั้งแต่ CUT ถึง PACK:

`PACK time - CUT time`

จะแสดงค่าได้เมื่อพบทั้ง CUT time และ PACK time หากขาดด้านใดด้านหนึ่งจะแสดง `-`

### Process Timeline

Timeline แสดง Process ทั้งหกตามลำดับ สีของจุดมีความหมายดังนี้:

- `Passed`: พบ Scan แล้วและไม่ใช่ Process ปัจจุบัน
- `Current`: Process ล่าสุดของ Barcode
- `Completed`: Process ล่าสุดคือ PACK
- `Not Reached`: ยังไม่พบ Scan

ข้อความเหนือ Timeline สรุป Process ที่ผ่านแล้ว Process ปัจจุบัน และเวลาตั้งแต่ CUT เมื่อวางเมาส์บนจุดจะแสดง First Scan ของ Process นั้น

## 9. วิธีระบุ Process ปัจจุบัน

ระบบสร้าง Barcode Summary แล้วตรวจ Process จากท้ายสายการผลิตย้อนกลับ:

`PACK → WAIT_FN → SEW → SM3 → SM2 → CUT`

Process ลำดับท้ายสุดที่พบข้อมูลจะถูกกำหนดเป็น `current_stage` วิธีนี้ทำให้ Barcode ที่มีข้อมูลข้ามขั้นถูกจัดให้อยู่ที่ Process ที่ไกลที่สุดที่พบ ไม่ได้บังคับว่าทุก Process ก่อนหน้าต้องมีข้อมูลครบ

## 10. แนวทางการใช้งาน

### ตรวจภาพรวมโรงงาน

1. เลือก Factory หรือ `ALL`
2. ตรวจ Completion Rate และ Avg. Aging
3. เปิด Production Flow เพื่อดู Qty และ WIP
4. เปิด Time & Waiting Analysis เพื่อดู Flow ที่ช้าและปริมาณงานรอ

### ตรวจ SO

1. เลือก Factory
2. เลือกหรือพิมพ์ SO NO
3. เปรียบเทียบ WIP ของแต่ละ Process
4. ตรวจ Waiting Barcode และ Bottleneck
5. เลือก Barcode ที่น่าสงสัยเพื่อดู Timeline

### ตรวจ Barcode

1. เลือก Barcode จากช่องด้านบน
2. เปิด Barcode Drill-down
3. ตรวจ Current Position และ Status
4. ตรวจ Minutes Since CUT
5. ดู Timeline และ First Scan ของแต่ละ Process

## 11. ข้อควรระวังในการตีความ

- WIP อาศัยความครบถ้วนของเวลา Scan หาก Process ถัดไป Scan ไม่เข้า ระบบจะมองว่ายังรออยู่
- Input และ Output ใช้สูตรตาม Process ส่วน WIP ใช้เงื่อนไขเวลา Scan จึงไม่ใช่ `Input - Output`
- จำนวน Barcode กับจำนวนชิ้นเป็นคนละหน่วย ต้องตรวจชื่อแกนและคำว่า `barcode` หรือ `pcs`
- Median เหมาะกับการดูค่ากลางเมื่อมีค่าผิดปกติ ส่วน Mode แสดงค่าที่ซ้ำบ่อยและอาจเป็น `0` หากข้อมูลจำนวนมากเกิดในเวลาใกล้กัน
- Outlier หมายถึงค่าที่สูงกว่าเกณฑ์ทางสถิติ ไม่ได้ยืนยันว่าเป็นข้อมูลผิดหรือปัญหาการผลิตเสมอไป
- CUT เวลา `00:00:00` ที่ไม่มี `upd_date` ยังแสดงใน Timeline แต่ไม่เข้าสถิติเวลารวมตามกฎที่กำหนด
- SM2 และ WAIT_FN เริ่มข้อมูลวันที่ `2026-07-09` ดังนั้นการวิเคราะห์ช่วงก่อนวันดังกล่าวจะไม่มีข้อมูลสอง Process นี้

## 12. สรุปสูตรหลัก

```text
Completion Rate = Completed Barcodes / Total Barcodes × 100

Open Barcodes = Total Barcodes - Completed Barcodes

Elapsed Minutes = Target Process Time - Source Process Time

Aging Minutes = Current Time - Current Process Time

Minutes Since CUT = Current Time - CUT Time

Total Lead Time = PACK Time - CUT Time

WIP (pcs) = SUM(Process Input Qty)
WIP Barcode Count = SUM(Barcode Count ของ Process ต้นทาง)
โดยใช้เงื่อนไขเดียวกันคือ Process Time มีค่า และ Next Process Time ไม่มีค่า
```

ไฟล์ `wip_by_so_no_query.sql` แยกผลลัพธ์เป็นคอลัมน์ `*_wip_pcs` และ `*_wip_barcode_count` โดยแต่ละ Process ใช้ Input Qty และจำนวนรายการ Barcode จาก Process ของตัวเอง จากนั้นใช้เวลาของ Process ต้นทางและ Process ถัดไปเป็นเงื่อนไขจัดกลุ่ม WIP ส่วน `total_wip_barcode_count` เป็นผลรวมจำนวนรายการ Barcode ที่เป็น WIP ทุก Process และไม่ได้นับ Barcode ทั้งหมดของ SO
