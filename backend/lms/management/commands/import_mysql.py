import pymysql
from django.core.management.base import BaseCommand
from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone
from lms.models import (
    Question, Choice, Test, TestQuestion, 
    SharedInstruction, TestPartInstruction
)

def parse_question_list(q_list_str):
    q_ids = []
    if not q_list_str:
        return q_ids
    # Remove whitespace
    s = "".join(q_list_str.split())
    # Split by comma
    parts = s.split(',')
    for part in parts:
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-')
                q_ids.extend(range(int(start), int(end) + 1))
            except ValueError:
                pass
        else:
            try:
                q_ids.append(int(part))
            except ValueError:
                pass
    return q_ids

class Command(BaseCommand):
    help = 'Chuyển đổi dữ liệu câu hỏi và đề thi từ MySQL (tracnghiem2018) vào Django LMS'

    def handle(self, *args, **options):
        self.stdout.write('--- KHỞI ĐẦU QUÁ TRÌNH IMPORT CSDL TỪ MYSQL ---')

        # 1. Tìm hoặc tạo user để làm người tạo
        creator = (
            User.objects.filter(is_superuser=True).first() or 
            User.objects.filter(is_staff=True).first() or 
            User.objects.first()
        )
        if not creator:
            # Tạo một user admin mặc định nếu CSDL hoàn toàn trống
            creator = User.objects.create_superuser(
                username='admin',
                email='admin@example.com',
                password='adminpassword'
            )
            self.stdout.write(self.style.SUCCESS('- Đã tạo tài khoản admin mặc định cho các bản ghi mới.'))

        # 2. Khởi tạo lời dẫn mặc định cho các phần thi tốt nghiệp
        self.stdout.write('Khởi tạo lời dẫn chuẩn cho các phần thi tốt nghiệp...')
        inst_part_1, _ = SharedInstruction.objects.get_or_create(
            title="Hướng dẫn Phần I (Thi tốt nghiệp)",
            defaults={
                'content': (
                    "**PHẦN I. Câu trắc nghiệm nhiều phương án lựa chọn.** "
                    "Thí sinh trả lời từ câu hỏi này. Mỗi câu hỏi chỉ chọn một phương án."
                )
            }
        )
        inst_part_2, _ = SharedInstruction.objects.get_or_create(
            title="Hướng dẫn Phần II (Thi tốt nghiệp)",
            defaults={
                'content': (
                    "**PHẦN II. Câu trắc nghiệm đúng sai.** "
                    "Trong mỗi ý a), b), c), d) ở mỗi câu, thí sinh chọn đúng hoặc sai."
                )
            }
        )
        inst_part_3, _ = SharedInstruction.objects.get_or_create(
            title="Hướng dẫn Phần III (Thi tốt nghiệp)",
            defaults={
                'content': (
                    "**PHẦN III. Câu trắc nghiệm trả lời ngắn.** "
                    "Thí sinh điền kết quả ngắn vào ô trống."
                )
            }
        )

        # 3. Kết nối CSDL MySQL
        self.stdout.write('Đang kết nối tới MySQL database (tracnghiem2018)...')
        try:
            connection = pymysql.connect(
                host='127.0.0.1',
                user='root',
                password='',
                database='tracnghiem2018',
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
            self.stdout.write(self.style.SUCCESS('Kết nối MySQL thành công!'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Kết nối MySQL thất bại: {e}'))
            return

        try:
            with connection.cursor() as cursor:
                # A. Lấy toàn bộ Choices để gom nhóm trong bộ nhớ (Tránh truy vấn lặp N+1)
                self.stdout.write('Đang tải câu trả lời từ MySQL...')
                cursor.execute("SELECT * FROM choices ORDER BY sort_order ASC, id ASC;")
                mysql_choices = cursor.fetchall()
                choices_by_question = {}
                for c in mysql_choices:
                    choices_by_question.setdefault(c['question_id'], []).append(c)

                # B. Lấy toàn bộ Questions từ MySQL
                self.stdout.write('Đang tải câu hỏi từ MySQL...')
                cursor.execute("SELECT * FROM questions ORDER BY id ASC;")
                mysql_questions = cursor.fetchall()

                # C. Lấy toàn bộ Exams từ MySQL
                self.stdout.write('Đang tải đề thi từ MySQL...')
                cursor.execute("SELECT * FROM exams ORDER BY id ASC;")
                mysql_exams = cursor.fetchall()

            # Bắt đầu giao dịch ghi CSDL an toàn của Django
            with transaction.atomic():
                self.stdout.write(f'Bắt đầu nạp {len(mysql_questions)} câu hỏi và {len(mysql_choices)} câu trả lời vào Django...')
                
                # Bảng ánh xạ ID: { ID_MySQL_Cũ: ID_Django_Mới }
                question_id_map = {}
                q_imported_count = 0
                c_imported_count = 0

                # 4. Import Questions & Choices
                for q_row in mysql_questions:
                    mysql_q_id = q_row['id']
                    mysql_q_type = q_row['question_type']
                    
                    # Xác định kiểu câu hỏi trong Django
                    if mysql_q_type == 'multiple':
                        # Trắc nghiệm 1 hoặc nhiều lựa chọn
                        associated_choices = choices_by_question.get(mysql_q_id, [])
                        correct_count = sum(1 for c in associated_choices if c['is_correct'])
                        # Nếu có nhiều hơn 1 phương án đúng -> Trắc nghiệm nhiều lựa chọn (Type 2), ngược lại -> Trắc nghiệm 1 lựa chọn (Type 1)
                        django_q_type = 2 if correct_count > 1 else 1
                    elif mysql_q_type == 'true_false':
                        # Đúng/Sai 4 ý
                        django_q_type = 3
                    elif mysql_q_type == 'short_answer':
                        # Trả lời ngắn
                        django_q_type = 4
                    else:
                        # Fallback
                        django_q_type = 1

                    # Tạo câu hỏi mới (Django tự tăng ID)
                    q_obj = Question.objects.create(
                        content=q_row['content'],
                        question_type=django_q_type,
                        is_public=True,
                        created_at=q_row['created_at'] or timezone.now(),
                        creator=creator
                    )
                    question_id_map[mysql_q_id] = q_obj.id
                    q_imported_count += 1

                    # Nạp câu trả lời liên kết
                    for c_row in choices_by_question.get(mysql_q_id, []):
                        is_correct = bool(c_row['is_correct'])
                        position = c_row['sort_order'] or 0

                        # Dành cho câu hỏi trả lời ngắn (Type 4)
                        if django_q_type == 4:
                            is_correct = False  # Case-insensitive
                            position = 0        # Keep spaces

                        Choice.objects.create(
                            question=q_obj,
                            content=c_row['choice_text'],
                            is_correct=is_correct,
                            position=position
                        )
                        c_imported_count += 1

                self.stdout.write(self.style.SUCCESS(
                    f'Nạp thành công: {q_imported_count} câu hỏi, {c_imported_count} câu trả lời.'
                ))

                # 5. Import Đề thi (Exams) & Liên kết câu hỏi (TestQuestions)
                self.stdout.write(f'Bắt đầu nạp {len(mysql_exams)} đề thi...')
                exams_imported_count = 0
                test_questions_count = 0

                for e_row in mysql_exams:
                    # Tạo đề thi mới
                    test_obj = Test.objects.create(
                        title=e_row['title'],
                        price=e_row['price'] or 0.0,
                        allow_practice=True,
                        is_official=True,
                        duration=60, # Mặc định 60 phút
                        test_type='STANDALONE',
                        creator=creator
                    )

                    # Phân tích danh sách câu hỏi
                    mysql_q_ids = parse_question_list(e_row['question_list'])
                    
                    parts_present = set()
                    order_idx = 1

                    for mq_id in mysql_q_ids:
                        django_q_id = question_id_map.get(mq_id)
                        if not django_q_id:
                            # Bỏ qua nếu câu hỏi này không tồn tại trong CSDL MySQL
                            continue

                        question = Question.objects.get(id=django_q_id)

                        # Thiết lập điểm số và Phần thi (Part) chuẩn hóa theo kiểu câu hỏi
                        if question.question_type in [1, 2]:
                            # Phần I: Trắc nghiệm 1/Nhiều lựa chọn (0.25 điểm mỗi câu)
                            part_number = 1
                            points = 0.25
                        elif question.question_type == 3:
                            # Phần II: Đúng/Sai 4 ý (1.0 điểm mỗi câu lớn)
                            part_number = 2
                            points = 1.0
                        elif question.question_type == 4:
                            # Phần III: Trả lời ngắn (0.5 điểm mỗi câu)
                            part_number = 3
                            points = 0.5
                        else:
                            part_number = 1
                            points = 1.0

                        TestQuestion.objects.create(
                            test=test_obj,
                            question=question,
                            points=points,
                            optional_type='NONE',
                            order_index=order_idx,
                            part_number=part_number
                        )
                        parts_present.add(part_number)
                        order_idx += 1
                        test_questions_count += 1

                    # Gán hướng dẫn phần thi chuẩn
                    for part_num in parts_present:
                        if part_num == 1:
                            inst_obj = inst_part_1
                        elif part_num == 2:
                            inst_obj = inst_part_2
                        elif part_num == 3:
                            inst_obj = inst_part_3
                        else:
                            continue

                        TestPartInstruction.objects.get_or_create(
                            test=test_obj,
                            part_number=part_num,
                            defaults={'instruction': inst_obj}
                        )

                    exams_imported_count += 1

                self.stdout.write(self.style.SUCCESS(
                    f'Nạp thành công: {exams_imported_count} đề thi, {test_questions_count} liên kết câu hỏi đề thi.'
                ))

            self.stdout.write(self.style.SUCCESS('--- QUÁ TRÌNH IMPORT MYSQL HOÀN THÀNH THÀNH CÔNG RỰC RỠ ---'))

        finally:
            connection.close()
