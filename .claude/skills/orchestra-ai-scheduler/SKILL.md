---
name: orchestra-ai-scheduler
description: ระบบนัดผ่าตัดและบริหารจัดการทรัพยากรห้องผ่าตัดอัจฉริยะด้วย Multi-Agent (ORchestraAI) ใช้สำหรับตรวจสอบสล็อตเวลา, ประเมินเวลาผ่าตัด, เช็กความพร้อมบุคลากร, เครื่องมือ CSSD, และห้องผ่าตัด พร้อมป้องกันตารางชนกัน 100% ด้วย Optimization Solver
---

# SKILL: ORchestraAI Operating Room Scheduling

## 1. Overview & Objectives
ORchestraAI เป็นระบบประสานงานทรัพยากรห้องผ่าตัดแบบ Multi-Agent ทำหน้าที่จัดการปัจจัย 3 ด้านหลัก ได้แก่ **1. ทีมงาน (Staff)**, **2. เครื่องมือและจ่ายกลาง (Equipment & CSSD)** และ **3. ห้องผ่าตัด (OR Facilities)** โดยมีเป้าหมายเพื่อหาช่วงเวลาจองที่ดีที่สุด ป้องกันการชนกัน และบริหารจัดการเคสฉุกเฉินแบบ Real-time

## 2. Multi-Agent Architecture & Responsibilities
เมื่อได้รับคำขอผ่าตัด Agent ทั้ง 5 ตัวจะทำงานร่วมกันตามลำดับดังนี้:
- **Intake & Triage Agent:** รับคำสั่ง (รองรับข้อความ/เสียงผ่าน NLP) วิเคราะห์ความเร่งด่วน (`ELECTIVE`, `URGENT`, `EMERGENCY`) และพยากรณ์เวลาผ่าตัดจากประวัติศัลยแพทย์
- **Staffing Agent:** ตรวจสอบตารางเวร ทักษะเฉพาะทาง และชั่วโมงการทำงาน (`max_weekly_hours`) เพื่อป้องกันความเหนื่อยล้า
- **Equipment & CSSD Agent:** ตรวจสอบสถานะเครื่องมือพิเศษ (เช่น หุ่นยนต์ผ่าตัด) และคำนวณเวลารออบฆ่าเชื้อ (`sterilization_mins`) ที่แผนกจ่ายกลาง
- **OR Facility Agent:** จับคู่หัตถการกับสเปกห้อง (`STANDARD`, `ROBOTIC_READY`, `HYBRID`) รวมถึงคำนวณเวลาทำความสะอาดห้อง (`turnover_mins`) และจองเตียงพักฟื้น (PACU/ICU)
- **Master Orchestrator Agent:** ศูนย์กลางรวบรวมข้อมูลทั้งหมด แล้วส่งพารามิเตอร์เข้า **Constraint Optimization Engine (Google OR-Tools CP-SAT)** เพื่อคำนวณตารางเวลาที่ไม่ชนกัน 100%

## 3. Core Database Constraints (Hard & Soft Constraints)
ในการคำนวณตารางเวลา ระบบต้องปฏิบัติตามเงื่อนไขเหล่านี้อย่างเคร่งครัด:
- **Hard Constraints (ห้ามละเมิดเด็ดขาด):**
  - ห้ามจองห้องผ่าตัดห้องเดิมในเวลาที่ทับซ้อนกัน (`EXCLUDE USING gist`)
  - ห้ามบุคลากรคนเดิมถูกจัดเข้ามากกว่า 1 ห้องในเวลาเดียวกัน
  - ห้ามใช้อุปกรณ์หรือชุดเครื่องมือชิ้นเดียวกันซ้อนกัน
  - เคสต้องอยู่ในช่วงเวลาปฏิบัติงาน (Shift) ของทีมแพทย์
- **Soft Constraints (ปรับให้เหมาะสมที่สุด):**
  - พยายามจัดเวลาให้ตรงกับความต้องการของแพทย์ให้มากที่สุด (`preferred_start`)
  - จัดเรียงเคสที่ใช้เครื่องมือคล้ายกันไว้ใกล้กันเพื่อลดเวลา Set-up

## 4. Execution Workflow (ขั้นตอนการทำงานของ Agent)
1. **Receive Request:** แปลงคำสั่งจองเป็นโครงสร้างข้อมูล (SurgeryCase)
2. **Parallel Scan:** สั่งตรวจสอบ Staff, Equipment และ OR พร้อมกันที่เวลาเป้าหมาย
3. **Negotiation & Conflict Resolution:** 
   - หากทรัพยากรว่างตรงกัน -> ล็อกตารางทันที
   - หากติดเงื่อนไข (เช่น เครื่องมืออยู่ระหว่างอบฆ่าเชื้อที่ CSSD) -> Master Orchestrator จะคำนวณหาช่วงเวลาถัดไปที่พร้อมสมบูรณ์
4. **Emergency Handling:** หากมีเคสฉุกเฉิน (`EMERGENCY`) เข้ามา ระบบจะคำนวณ Ripple Effect และขยับเคสที่ไม่เร่งด่วนออกไปอัตโนมัติพร้อมแจ้งเตือนทีมงาน

## 5. Python Optimization Engine Reference
เมื่อต้องการรันคำนวณตารางด้วยโค้ด ให้เรียกใช้คลาสหลักจากโมดูลคำนวณ:
```python
from orchestra_engine import ORchestraOptimizer, OperatingRoom, EquipmentUnit, StaffMember, SurgeryCase

# ตัวอย่างการเรียกใช้งาน Solver
optimizer = ORchestraOptimizer(rooms, equipments, staff)
optimizer.solve_schedule(cases, "Scenario Name")
```