# HocTin LMS & Judge Sync System

Hệ thống Quản lý Học tập & Luyện thi Lập trình tích hợp đồng bộ Online Judge.

## Tính năng chính
- **LMS**: Quản lý khóa học, bài học (Markdown + LaTeX).
- **Exam System**: Đề thi nhiều phần, 4 loại câu hỏi, chấm điểm phân tầng, nhóm tự chọn TC1/TC2.
- **Judge Sync**: Đồng bộ kết quả từ Codeforces/VNOJ.
- **Wallet**: Thanh toán nội bộ, nạp tiền offline qua QR/Chuyển khoản.
- **Docker**: Sẵn sàng triển khai với Docker Compose.

## Hướng dẫn cài đặt (Cục bộ - Dev)
1. Cài đặt các thư viện: `pip install -r requirements.txt`
2. Chạy migration: `cd backend && python manage.py migrate`
3. Khởi tạo dữ liệu mẫu: `python manage.py seed_data`
4. Chạy server: `python manage.py runserver`

## Triển khai bằng Docker
1. Chạy lệnh: `docker-compose up --build`
2. Truy cập: `http://localhost:8000`

## Tài khoản mặc định
- **Admin**: `admin` / `admin123`
