from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
import datetime
from lms.models import Course, Lesson, Question, Choice, Test, SharedInstruction, TestPartInstruction, TestQuestion, Profile, TestRegulation

class Command(BaseCommand):
    help = 'Tạo dữ liệu mẫu cho hệ thống HocTin LMS'

    def handle(self, *args, **kwargs):
        self.stdout.write('Đang tạo dữ liệu mẫu...')
        
        # 1. Tạo admin nếu chưa có
        admin, created = User.objects.get_or_create(username='admin')
        if created:
            admin.set_password('admin123')
            admin.is_superuser = True
            admin.is_staff = True
            admin.save()
            self.stdout.write('- Đã tạo user admin/admin123')
        
        # Đảm bảo Profile của admin có tiền ví để test thanh toán
        profile, _ = Profile.objects.get_or_create(user=admin)
        profile.wallet_balance = 50000.00
        profile.save()
        self.stdout.write('- Đã nạp 50.000đ vào ví tài khoản admin')

        # 2. Tạo Quy chế phòng thi mẫu
        regulation, _ = TestRegulation.objects.get_or_create(
            title="Quy chế thi chính thức HocTin LMS",
            defaults={
                'content': (
                    "1. Nghiêm cấm mọi hành vi gian lận, sao chép code hoặc nhờ người khác làm bài hộ.\n"
                    "2. Trong thời gian làm bài chính thức, học sinh không được phép chuyển tab trình duyệt quá 3 lần. Hệ thống tự động ghi nhận và tự động nộp bài nếu vi phạm.\n"
                    "3. Các bài thi sẽ được quét độ tương đồng mã nguồn tự động sau khi kết thúc kỳ thi.\n"
                    "4. Thí sinh phải tự đảm bảo nguồn điện ổn định và kết nối Internet tốt nhất."
                )
            }
        )
        self.stdout.write('- Đã khởi tạo Quy chế phòng thi mẫu')

        # 3. Tạo khóa học mẫu
        course, _ = Course.objects.get_or_create(
            title='Lập trình C++ từ cơ bản đến nâng cao',
            defaults={
                'description': 'Khóa học dành cho người mới bắt đầu học lập trình C++ và thuật toán.',
                'price': 0,
                'learning_mode': 'SEQUENTIAL'
            }
        )
        
        Lesson.objects.get_or_create(
            course=course,
            title='Giới thiệu về biến và kiểu dữ liệu',
            defaults={'content': 'Nội dung bài học về biến...', 'order_index': 1}
        )

        # 4. Tạo Đề 1: Luyện tập tự do (Đề mẫu cũ)
        test_practice, _ = Test.objects.get_or_create(
            title='Đề kiểm tra năng lực Thuật toán - Đề số 1',
            defaults={
                'price': 0,
                'is_official': False,
                'allow_practice': True
            }
        )
        
        inst_practice_i, _ = SharedInstruction.objects.get_or_create(
            title="Lời dẫn Phần I - Đề luyện tập 1",
            defaults={'content': 'Phần này gồm các câu hỏi trắc nghiệm kiến thức cơ bản.'}
        )
        TestPartInstruction.objects.get_or_create(
            test=test_practice,
            part_number=1,
            defaults={'instruction': inst_practice_i}
        )

        # 5. Tạo Đề 2: Đề thi chính thức (Có phí 10.000đ, sắp diễn ra sau 3 phút để test countdown)
        test_official, _ = Test.objects.get_or_create(
            title='Kỳ thi lập trình ACM/ICPC vòng sơ loại',
            defaults={
                'price': 10000.00,
                'is_official': True,
                'allow_practice': False,
                'start_time': timezone.now() + datetime.timedelta(minutes=3),
                'end_time': timezone.now() + datetime.timedelta(hours=2),
                'duration': 120,
                'regulation': regulation
            }
        )
        self.stdout.write('- Đã tạo Đề thi chính thức có thu phí')
        
        inst_official_i, _ = SharedInstruction.objects.get_or_create(
            title="Lời dẫn Phần I - Đề chính thức ACM",
            defaults={'content': 'Phần thi bắt buộc gồm câu hỏi thuật toán cơ bản.'}
        )
        TestPartInstruction.objects.get_or_create(
            test=test_official,
            part_number=1,
            defaults={'instruction': inst_official_i}
        )

        # 6. Tạo câu hỏi và liên kết vào cả 2 đề thi
        
        # Câu hỏi 1: Single Choice
        q1, _ = Question.objects.get_or_create(
            content='Độ phức tạp của thuật toán sắp xếp QuickSort trong trường hợp tốt nhất là gì?',
            defaults={'question_type': 1}
        )
        Choice.objects.get_or_create(question=q1, content='O(n)', defaults={'is_correct': False, 'position': 1})
        Choice.objects.get_or_create(question=q1, content='O(n log n)', defaults={'is_correct': True, 'position': 2})
        Choice.objects.get_or_create(question=q1, content='O(n^2)', defaults={'is_correct': False, 'position': 3})
        
        TestQuestion.objects.get_or_create(test=test_practice, question=q1, part_number=1, defaults={'points': 1.0, 'order_index': 1})
        TestQuestion.objects.get_or_create(test=test_official, question=q1, part_number=1, defaults={'points': 1.0, 'order_index': 1})

        # Câu hỏi 2: Đúng/Sai 4 ý
        q3, _ = Question.objects.get_or_create(
            content='Xét các phát biểu sau về ngôn ngữ C++:',
            defaults={'question_type': 3}
        )
        Choice.objects.get_or_create(question=q3, content='C++ là ngôn ngữ lập trình hướng đối tượng.', defaults={'is_correct': True, 'position': 1})
        Choice.objects.get_or_create(question=q3, content='Hàm main() có thể trả về kiểu void.', defaults={'is_correct': False, 'position': 2})
        Choice.objects.get_or_create(question=q3, content='Con trỏ trong C++ không thể trỏ tới một con trỏ khác.', defaults={'is_correct': False, 'position': 3})
        Choice.objects.get_or_create(question=q3, content='C++ hỗ trợ đa kế thừa.', defaults={'is_correct': True, 'position': 4})
        
        TestQuestion.objects.get_or_create(test=test_practice, question=q3, part_number=1, defaults={'points': 2.0, 'order_index': 2})
        TestQuestion.objects.get_or_create(test=test_official, question=q3, part_number=1, defaults={'points': 2.0, 'order_index': 2})

        # Phần II của Đề 1 (tự chọn)
        inst_practice_ii, _ = SharedInstruction.objects.get_or_create(
            title="Lời dẫn Phần II - Đề luyện tập 1",
            defaults={'content': 'Chọn 1 trong 2 nhóm câu hỏi sau để làm bài.'}
        )
        TestPartInstruction.objects.get_or_create(
            test=test_practice,
            part_number=2,
            defaults={'instruction': inst_practice_ii}
        )
        
        qtc1, _ = Question.objects.get_or_create(
            content='Giải thuật tìm kiếm nhị phân yêu cầu mảng phải được sắp xếp. (TC1)',
            defaults={'question_type': 1}
        )
        Choice.objects.get_or_create(question=qtc1, content='Đúng', defaults={'is_correct': True})
        Choice.objects.get_or_create(question=qtc1, content='Sai', defaults={'is_correct': False})
        TestQuestion.objects.get_or_create(test=test_practice, question=qtc1, part_number=2, defaults={'points': 1.0, 'optional_type': 'TC1', 'order_index': 3})

        qtc2, _ = Question.objects.get_or_create(
            content='Cấu trúc dữ liệu Stack hoạt động theo nguyên lý FIFO. (TC2)',
            defaults={'question_type': 1}
        )
        Choice.objects.get_or_create(question=qtc2, content='Đúng', defaults={'is_correct': False})
        Choice.objects.get_or_create(question=qtc2, content='Sai', defaults={'is_correct': True})
        TestQuestion.objects.get_or_create(test=test_practice, question=qtc2, part_number=2, defaults={'points': 1.0, 'optional_type': 'TC2', 'order_index': 4})

        self.stdout.write(self.style.SUCCESS('--- Đã tạo dữ liệu mẫu thành công ---'))
