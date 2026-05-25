from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
import uuid

# 1. Quản lý Người dùng & Ví tiền
class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    vnoj_username = models.CharField(max_length=100, blank=True)
    codeforces_username = models.CharField(max_length=100, blank=True)
    dmoj_username = models.CharField(max_length=100, blank=True)
    
    # Quyền hạn tạo nội dung cho Staff/Giáo viên
    can_create_questions = models.BooleanField(default=False)
    can_create_exams = models.BooleanField(default=False)
    can_create_courses = models.BooleanField(default=False)
    
    def __str__(self):
        return self.user.username

class BankAccount(models.Model):
    bank_name = models.CharField(max_length=100, verbose_name="Tên ngân hàng")
    bank_code = models.CharField(max_length=20, verbose_name="Mã ngân hàng (ví dụ: VCB, MB, TCB)")
    account_number = models.CharField(max_length=50, verbose_name="Số tài khoản")
    account_holder = models.CharField(max_length=100, verbose_name="Chủ tài khoản")
    is_active = models.BooleanField(default=True, verbose_name="Đang hoạt động")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tài khoản ngân hàng"
        verbose_name_plural = "Các tài khoản ngân hàng"

    def __str__(self):
        return f"{self.bank_name} - {self.account_number} ({self.account_holder})"


class WalletTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('DEPOSIT', 'Nạp tiền'),
        ('PAYMENT', 'Thanh toán'),
    )
    STATUS_CHOICES = (
        ('PENDING', 'Chờ duyệt'),
        ('APPROVED', 'Đã duyệt'),
        ('REJECTED', 'Từ chối'),
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    proof_image = models.FileField(upload_to='transactions/', blank=True, null=True)
    
    # Liên kết trực tiếp sản phẩm thanh toán offline
    course = models.ForeignKey('Course', on_delete=models.SET_NULL, blank=True, null=True, related_name='transactions')
    course_bundle = models.ForeignKey('CourseBundle', on_delete=models.SET_NULL, blank=True, null=True, related_name='transactions')
    test = models.ForeignKey('Test', on_delete=models.SET_NULL, blank=True, null=True, related_name='transactions')
    test_bundle = models.ForeignKey('TestBundle', on_delete=models.SET_NULL, blank=True, null=True, related_name='transactions')
    bank_account = models.ForeignKey('BankAccount', on_delete=models.SET_NULL, blank=True, null=True, related_name='transactions', verbose_name="Ngân hàng thụ hưởng")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def approve(self):
        """
        Centrally approves a transaction.
        If it's a DEPOSIT, increases the user's profile wallet balance.
        If it's a PAYMENT, grants CourseOwnership or TestOwnership access.
        """
        from django.db import transaction
        from django.utils import timezone
        
        if self.status != 'PENDING':
            return False
            
        with transaction.atomic():
            if self.transaction_type == 'DEPOSIT':
                profile = self.user.profile
                profile.wallet_balance += self.amount
                profile.save()
            else:
                # Direct product payment approvals - Grant access atomically
                if self.course:
                    expires_at = None
                    if self.course.duration_days and self.course.duration_days > 0:
                        expires_at = timezone.now() + timezone.timedelta(days=self.course.duration_days)
                    ownership, created = CourseOwnership.objects.get_or_create(
                        user=self.user, 
                        course=self.course,
                        defaults={'expires_at': expires_at}
                    )
                    if not created:
                        ownership.expires_at = expires_at
                        ownership.purchased_at = timezone.now()
                        ownership.save()
                elif self.course_bundle:
                    for course in self.course_bundle.courses.all():
                        expires_at = None
                        if course.duration_days and course.duration_days > 0:
                            expires_at = timezone.now() + timezone.timedelta(days=course.duration_days)
                        ownership, created = CourseOwnership.objects.get_or_create(
                            user=self.user, 
                            course=course,
                            defaults={'expires_at': expires_at}
                        )
                        if not created:
                            ownership.expires_at = expires_at
                            ownership.purchased_at = timezone.now()
                            ownership.save()
                elif self.test:
                    TestOwnership.objects.update_or_create(
                        user=self.user, 
                        test=self.test, 
                        defaults={'registered_at': timezone.now(), 'agreed_rules': True}
                    )
                elif self.test_bundle:
                    for test in self.test_bundle.tests.all():
                        TestOwnership.objects.update_or_create(
                            user=self.user, 
                            test=test, 
                            defaults={'registered_at': timezone.now(), 'agreed_rules': True}
                        )
            self.status = 'APPROVED'
            self.save()
        return True

# 2. Quản lý Khóa học
class Course(models.Model):
    LEARNING_MODE = (
        ('SEQUENTIAL', 'Tuần tự'),
        ('FREE', 'Tự do'),
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    learning_mode = models.CharField(max_length=15, choices=LEARNING_MODE, default='SEQUENTIAL')
    thumbnail = models.FileField(upload_to='courses/', blank=True, null=True)
    duration_days = models.PositiveIntegerField(default=30, null=True, blank=True, verbose_name="Thời hạn học (ngày)", help_text="Số ngày được học kể từ lúc mua/duyệt. Nhập 0 hoặc để trống nếu muốn học trọn đời.")
    created_at = models.DateTimeField(auto_now_add=True)
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_courses', verbose_name="Người tạo")

    def __str__(self):
        return self.title

class CourseBundle(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    courses = models.ManyToManyField(Course, related_name='bundles')
    
    def __str__(self):
        return self.title

class Lesson(models.Model):
    TYPE_CHOICES = (
        ('THEORY', 'Bài học lý thuyết'),
        ('EXERCISE', 'Bài tập thực hành'),
        ('TEST', 'Bài kiểm tra / Đề thi')
    )
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='lessons')
    title = models.CharField(max_length=255)
    lesson_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='THEORY', verbose_name="Loại bài học")
    content = models.TextField(blank=True, verbose_name="Nội dung lý thuyết (Markdown)")
    video_url = models.URLField(blank=True, verbose_name="Link video bài học")
    order_index = models.PositiveIntegerField(default=1, verbose_name="Thứ tự bài học")
    external_judge_link = models.URLField(blank=True, help_text="Link bài tập ngoại vi (VNOJ, Codeforces...)")
    external_problem_code = models.CharField(max_length=50, blank=True, help_text="Mã bài tập (ví dụ: 123A)")
    test = models.ForeignKey('Test', on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Đề thi liên kết")
    
    class Meta:
        ordering = ['order_index']

# 3. Quản lý Ngân hàng Câu hỏi
class QuestionGroup(models.Model):
    name = models.CharField(max_length=255)
    
    def __str__(self):
        return self.name

class EquivalentQuestionGroup(models.Model):
    name = models.CharField(max_length=255, verbose_name="Tên nhóm câu hỏi tương đương")
    
    def __str__(self):
        return self.name

class Question(models.Model):
    TYPE_CHOICES = (
        (1, 'Trắc nghiệm 1 lựa chọn'),
        (2, 'Trắc nghiệm nhiều lựa chọn'),
        (3, 'Đúng/Sai 4 ý'),
        (4, 'Trả lời ngắn'),
    )
    group = models.ForeignKey(QuestionGroup, on_delete=models.SET_NULL, null=True, blank=True)
    equivalent_group = models.ForeignKey(EquivalentQuestionGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='questions', verbose_name="Nhóm câu hỏi tương đương")
    content = models.TextField() # Markdown + LaTeX
    question_type = models.IntegerField(choices=TYPE_CHOICES)
    is_public = models.BooleanField(default=True, verbose_name="Công khai (Học sinh có thể xem)")
    created_at = models.DateTimeField(auto_now_add=True)
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_questions', verbose_name="Người tạo")

    def __str__(self):
        return f"[{self.get_question_type_display()}] {self.content[:50]}"

class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='choices')
    content = models.TextField()
    is_correct = models.BooleanField(default=False)
    position = models.IntegerField(default=1) # Dùng để sắp xếp trước khi đảo
    
    class Meta:
        ordering = ['position']

# 4. Quản lý Đề thi
class TestRegulation(models.Model):
    title = models.CharField(max_length=255, verbose_name="Tiêu đề quy chế")
    content = models.TextField(verbose_name="Nội dung quy chế")
    
    def __str__(self):
        return self.title


def int_to_roman(n):
    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syb = [
        "M", "CM", "D", "CD",
        "C", "XC", "L", "XL",
        "X", "IX", "V", "IV",
        "I"
    ]
    roman_num = ''
    i = 0
    while  n > 0:
        for _ in range(n // val[i]):
            roman_num += syb[i]
            n -= val[i]
        i += 1
    return roman_num


class TestPartProxy:
    def __init__(self, test, part_number):
        self.test = test
        self.part_number = part_number

    @property
    def id(self):
        inst = self.test.part_instructions.filter(part_number=self.part_number).first()
        return inst.id if inst else self.part_number

    @property
    def title_roman(self):
        return int_to_roman(self.part_number)

    @property
    def questions(self):
        return self.test.questions.filter(part_number=self.part_number).order_by('order_index')


class TestPartProxyQuerySet:
    def __init__(self, test):
        self.test = test
        q_part_nums = set(self.test.questions.values_list('part_number', flat=True))
        inst_part_nums = set(self.test.part_instructions.values_list('part_number', flat=True))
        self.part_numbers = sorted(list(q_part_nums | inst_part_nums))

    def all(self):
        return [TestPartProxy(self.test, num) for num in self.part_numbers]

    def count(self):
        return len(self.part_numbers)

    def __len__(self):
        return len(self.part_numbers)

    def __iter__(self):
        return iter(self.all())

    def exists(self):
        return len(self.part_numbers) > 0


class Test(models.Model):
    TYPE_CHOICES = (
        ('STANDALONE', 'Đề thi riêng lẻ'),
        ('LESSON_ONLY', 'Đề thi gắn vào bài học'),
    )
    title = models.CharField(max_length=255)
    short_description = models.CharField(max_length=500, blank=True, default='', verbose_name="Mô tả ngắn về đề thi")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(default=60, help_text="Thời gian làm bài (phút)")
    allow_practice = models.BooleanField(default=True)
    is_official = models.BooleanField(default=False, verbose_name="Kỳ thi chính thức")
    shuffle_parts = models.BooleanField(default=False, verbose_name="Đảo ngẫu nhiên các phần", help_text="Nếu đề thi có nhiều phần, cho phép đảo ngẫu nhiên các phần đó.")
    test_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default='STANDALONE',
        verbose_name="Loại đề thi"
    )
    regulation = models.ForeignKey(TestRegulation, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Quy chế phòng thi")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_tests', verbose_name="Người tạo")
    
    def __str__(self):
        return self.title

    @property
    def parts(self):
        return TestPartProxyQuerySet(self)

    @property
    def total_possible_points(self):
        try:
            if hasattr(self, 'dynamictest') and self.dynamictest:
                return self.dynamictest.total_points
        except Exception:
            pass
        total = 0
        for tq in self.questions.all():
            total += tq.points
        return total

class DynamicTest(Test):
    max_questions = models.PositiveIntegerField(default=40, verbose_name="Số câu tối đa")
    total_points = models.FloatField(default=10.0, verbose_name="Tổng điểm đề thi")
    dynamic_group_rules = models.TextField(blank=True, null=True, verbose_name="Quy tắc chọn từ nhóm (ví dụ 7:3, 9:2)")
    dynamic_question_ids = models.TextField(blank=True, null=True, verbose_name="Danh sách ID câu hỏi chọn trực tiếp")

    class Meta:
        verbose_name = "Đề thi động"
        verbose_name_plural = "Đề thi động"

class TestBundle(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    tests = models.ManyToManyField(Test, related_name='bundles')

# 1. Kho lưu trữ lời dẫn dùng chung (tránh dữ liệu trùng lặp)
class SharedInstruction(models.Model):
    title = models.CharField(max_length=255, verbose_name="Tên gợi nhớ của lời dẫn")
    content = models.TextField(verbose_name="Nội dung lời dẫn (Markdown + LaTeX)")

    def __str__(self):
        return self.title

# 2. Bảng gán lời dẫn trước từng phần của đề thi
class TestPartInstruction(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='part_instructions')
    part_number = models.PositiveIntegerField(verbose_name="Phần số (1, 2, 3...)")
    instruction = models.ForeignKey(SharedInstruction, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Lời dẫn chung")
    
    class Meta:
        unique_together = ('test', 'part_number')
        ordering = ['part_number']

    def __str__(self):
        return f"{self.test.title} - Phần {self.part_number}"

# 3. Bảng gán câu hỏi trực tiếp vào đề thi
class TestQuestion(models.Model):
    OPTIONAL_CHOICES = (
        ('NONE', 'Bắt buộc'),
        ('TC1', 'Tự chọn 1'),
        ('TC2', 'Tự chọn 2')
    )
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='questions', null=True, blank=True)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    points = models.FloatField(default=1.0)
    optional_type = models.CharField(max_length=5, choices=OPTIONAL_CHOICES, default='NONE')
    order_index = models.IntegerField(default=1)
    part_number = models.PositiveIntegerField(default=1, verbose_name="Thuộc phần mấy (1, 2, 3...)")

# 5. Lượt làm bài & Kết quả
class Attempt(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    is_official = models.BooleanField(default=False)
    total_score = models.FloatField(default=0)
    shuffled_data = models.JSONField(default=dict, help_text="Lưu thứ tự đáp án đã đảo")
    left_page_count = models.PositiveIntegerField(default=0, verbose_name="Số lần rời khỏi trang")
    left_page_time = models.PositiveIntegerField(default=0, verbose_name="Tổng thời gian rời khỏi trang (giây)")
    
    def __str__(self):
        return f"{self.user.username} - {self.test.title}"

class AttemptAnswer(models.Model):
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name='answers')
    test_question = models.ForeignKey(TestQuestion, on_delete=models.CASCADE, null=True, blank=True)
    question = models.ForeignKey(Question, on_delete=models.CASCADE, null=True, blank=True)
    points = models.FloatField(default=1.0)
    selected_choices = models.ManyToManyField(Choice, blank=True) # Cho loại 1, 2, 3
    short_answer = models.CharField(max_length=255, blank=True) # Cho loại 4
    is_correct_tf = models.JSONField(default=list, help_text="Lưu trạng thái Đúng/Sai cho 4 ý của loại 3")
    score_earned = models.FloatField(default=0)

# 6. Quyền sở hữu
class CourseOwnership(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    purchased_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Ngày hết hạn", help_text="Thời điểm hết hạn học khóa học này. Để trống nghĩa là học trọn đời.")

    class Meta:
        unique_together = ('user', 'course')

    @property
    def is_expired(self):
        if self.expires_at is None:
            return False
        from django.utils import timezone
        return timezone.now() > self.expires_at

    @classmethod
    def has_active_ownership(cls, user, course):
        if not user.is_authenticated:
            return False
        # Cho phép admin, staff hoặc người tạo khóa học luôn có quyền sở hữu để học thử
        if user.is_superuser or user.is_staff or course.creator == user:
            return True
        from django.utils import timezone
        return cls.objects.filter(
            user=user,
            course=course
        ).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now())
        ).exists()

class TestOwnership(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    purchased_at = models.DateTimeField(auto_now_add=True)
    registered_at = models.DateTimeField(null=True, blank=True, verbose_name="Thời điểm đăng ký")
    agreed_rules = models.BooleanField(default=False, verbose_name="Đồng ý nội quy")

    class Meta:
        unique_together = ('user', 'test')

class LessonProgress(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'lesson')
