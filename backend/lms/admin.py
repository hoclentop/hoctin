from django.contrib import admin
from .models import (
    Profile, WalletTransaction, Course, CourseBundle, Lesson,
    QuestionGroup, EquivalentQuestionGroup, Question, Choice, Test, DynamicTest, TestBundle, SharedInstruction,
    TestPartInstruction, TestQuestion, Attempt, AttemptAnswer, TestRegulation, TestOwnership,
    BankAccount, CourseOwnership, Classroom, ClassroomMembership, ClassroomItem
)

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'wallet_balance', 'vnoj_username', 'codeforces_username']
    search_fields = ['user__username', 'vnoj_username']

@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ['bank_name', 'bank_code', 'account_number', 'account_holder', 'is_active', 'created_at']
    list_filter = ['is_active', 'bank_name']
    search_fields = ['bank_name', 'account_number', 'account_holder']

@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ['user', 'amount', 'transaction_type', 'status', 'bank_account', 'created_at']
    list_filter = ['status', 'transaction_type', 'bank_account']
    actions = ['approve_transaction']

    def approve_transaction(self, request, queryset):
        for tx in queryset.filter(status='PENDING'):
            tx.approve()
    approve_transaction.short_description = "Phê duyệt các giao dịch đang chờ"

    def save_model(self, request, obj, form, change):
        if change:
            # Check if status has transitioned from PENDING to APPROVED
            old_obj = WalletTransaction.objects.get(pk=obj.pk)
            if old_obj.status == 'PENDING' and obj.status == 'APPROVED':
                # Revert to PENDING to let approve() execute full logic and save
                obj.status = 'PENDING'
                obj.approve()
                return
        super().save_model(request, obj, form, change)

class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 1
    fields = ['title', 'lesson_type', 'order_index', 'test']

@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ['title', 'course', 'lesson_type', 'order_index', 'test']
    list_filter = ['course', 'lesson_type']

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ['title', 'price', 'duration_days', 'learning_mode', 'creator']
    list_filter = ['learning_mode']
    inlines = [LessonInline]
    exclude = ['creator']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(creator=request.user)

    def save_model(self, request, obj, form, change):
        if not change or not obj.creator:
            obj.creator = request.user
        super().save_model(request, obj, form, change)

@admin.register(CourseOwnership)
class CourseOwnershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'course', 'purchased_at', 'expires_at', 'is_expired_display']
    list_filter = ['course']
    search_fields = ['user__username', 'course__title']

    def is_expired_display(self, obj):
        return obj.is_expired
    is_expired_display.boolean = True
    is_expired_display.short_description = "Đã hết hạn?"

@admin.register(CourseBundle)
class CourseBundleAdmin(admin.ModelAdmin):
    list_display = ['title', 'price']
    filter_horizontal = ['courses']

class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 4

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ['content_summary', 'question_type', 'group', 'equivalent_group', 'is_public', 'creator', 'edit_on_frontend']
    list_filter = ['question_type', 'group', 'equivalent_group', 'is_public']
    inlines = [ChoiceInline]
    exclude = ['creator']

    def edit_on_frontend(self, obj):
        from django.urls import reverse
        from django.utils.safestring import mark_safe
        url = reverse('edit_question', args=[obj.id])
        return mark_safe(f'<a class="button" href="{url}" style="background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%); color: white; padding: 5px 10px; border-radius: 8px; font-weight: bold; font-size: 11px; text-decoration: none; box-shadow: 0 2px 4px rgba(79, 70, 229, 0.25);">Sửa trên Frontend</a>')
    edit_on_frontend.short_description = "Thao tác"

    def content_summary(self, obj):
        return obj.content[:50]

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return super().has_change_permission(request, obj)
        return request.user.is_superuser or obj.creator == request.user

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return super().has_delete_permission(request, obj)
        return request.user.is_superuser or obj.creator == request.user

    def save_model(self, request, obj, form, change):
        if not change or not obj.creator:
            obj.creator = request.user
        super().save_model(request, obj, form, change)

    def response_add(self, request, obj, post_url_continue=None):
        response = super().response_add(request, obj, post_url_continue)
        if "_addanother" in request.POST and obj.group_id:
            from django.http import HttpResponseRedirect
            if isinstance(response, HttpResponseRedirect):
                response['Location'] += f"?group={obj.group_id}"
        return response

    def response_change(self, request, obj):
        response = super().response_change(request, obj)
        if "_addanother" in request.POST and obj.group_id:
            from django.http import HttpResponseRedirect
            if isinstance(response, HttpResponseRedirect):
                response['Location'] += f"?group={obj.group_id}"
        return response

@admin.register(QuestionGroup)
class QuestionGroupAdmin(admin.ModelAdmin):
    pass

from django import forms
import re

class EquivalentQuestionGroupForm(forms.ModelForm):
    question_ids = forms.CharField(
        label="ID các câu hỏi bổ sung",
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3, 
            'placeholder': 'Nhập các ID câu hỏi, phân cách bởi dấu cách, dấu phẩy hoặc dấu tab. Ví dụ: 12, 15, 29'
        }),
        help_text="Nhập danh sách ID các câu hỏi muốn bổ sung vào nhóm tương đương này. Các ID phân tách bởi dấu cách, dấu phẩy hoặc dấu tab."
    )

    class Meta:
        model = EquivalentQuestionGroup
        fields = ['name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            existing_qs = self.instance.questions.all()
            if existing_qs.exists():
                existing_list = [f"#{q.id}" for q in existing_qs]
                self.fields['question_ids'].help_text += f"<br><span style='color: #4f46e5;'><strong>Các câu hỏi hiện có trong nhóm:</strong> {', '.join(existing_list)}</span>"

    def clean_question_ids(self):
        question_ids_str = self.cleaned_data.get('question_ids', '')
        if not question_ids_str:
            return []
        
        raw_ids = re.split(r'[\s,\t\r\n]+', question_ids_str)
        cleaned_ids = []
        invalid_format_ids = []
        for rid in raw_ids:
            rid = rid.strip()
            if not rid:
                continue
            if rid.isdigit():
                cleaned_ids.append(int(rid))
            else:
                invalid_format_ids.append(rid)
                
        if invalid_format_ids:
            raise forms.ValidationError(
                f"Các giá trị sau không phải là ID hợp lệ (phải là số nguyên): {', '.join(invalid_format_ids)}"
            )
            
        if cleaned_ids:
            existing_ids = set(Question.objects.filter(id__in=cleaned_ids).values_list('id', flat=True))
            missing_ids = set(cleaned_ids) - existing_ids
            if missing_ids:
                raise forms.ValidationError(
                    f"Các ID câu hỏi sau không tồn tại trong hệ thống: {', '.join(map(str, missing_ids))}"
                )
                
        return cleaned_ids

@admin.register(EquivalentQuestionGroup)
class EquivalentQuestionGroupAdmin(admin.ModelAdmin):
    form = EquivalentQuestionGroupForm
    list_display = ['name', 'get_questions_count']
    search_fields = ['name']

    def get_questions_count(self, obj):
        return obj.questions.count()
    get_questions_count.short_description = "Số câu hỏi"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        question_ids = form.cleaned_data.get('question_ids', [])
        if question_ids:
            Question.objects.filter(id__in=question_ids).update(equivalent_group=obj)

class TestPartInstructionInline(admin.TabularInline):
    model = TestPartInstruction
    extra = 1

@admin.register(TestRegulation)
class TestRegulationAdmin(admin.ModelAdmin):
    list_display = ['title']
    search_fields = ['title']

@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ['title', 'price', 'short_description', 'start_time', 'end_time', 'is_official', 'shuffle_parts', 'creator']
    list_filter = ['is_official', 'shuffle_parts']
    search_fields = ['title']
    inlines = [TestPartInstructionInline]
    exclude = ['creator']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(creator=request.user)

    def save_model(self, request, obj, form, change):
        if not change or not obj.creator:
            obj.creator = request.user
        super().save_model(request, obj, form, change)

@admin.register(SharedInstruction)
class SharedInstructionAdmin(admin.ModelAdmin):
    list_display = ['title', 'content']

@admin.register(TestPartInstruction)
class TestPartInstructionAdmin(admin.ModelAdmin):
    list_display = ['test', 'part_number', 'instruction']

@admin.register(TestQuestion)
class TestQuestionAdmin(admin.ModelAdmin):
    list_display = ['test', 'question', 'part_number', 'points', 'optional_type', 'order_index']
    list_filter = ['optional_type', 'part_number']

@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ['user', 'test', 'total_score', 'start_time', 'is_official', 'left_page_count', 'left_page_time']
    list_filter = ['is_official', 'test']

@admin.register(TestOwnership)
class TestOwnershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'test', 'registered_at', 'agreed_rules', 'purchased_at']
    list_filter = ['agreed_rules', 'test']
    search_fields = ['user__username', 'test__title']

@admin.register(TestBundle)
class TestBundleAdmin(admin.ModelAdmin):
    list_display = ['title', 'price']
    filter_horizontal = ['tests']
    search_fields = ['title']


class DynamicTestForm(forms.ModelForm):
    class Meta:
        model = DynamicTest
        fields = '__all__'

    def clean_dynamic_group_rules(self):
        rules_str = self.cleaned_data.get('dynamic_group_rules', '')
        if not rules_str:
            return rules_str
            
        raw_rules = re.split(r'[\s,;\t]+', rules_str)
        for rr in raw_rules:
            rr = rr.strip()
            if not rr:
                continue
            if ':' not in rr:
                raise forms.ValidationError(f"Quy tắc '{rr}' không hợp lệ. Phải có định dạng 'ID_Nhom:So_Cau' (ví dụ: 7:3).")
            parts = rr.split(':')
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise forms.ValidationError(f"Quy tắc '{rr}' không hợp lệ. Cả ID nhóm và Số câu phải là số nguyên dương.")
            g_id = int(parts[0])
            count = int(parts[1])
            if not QuestionGroup.objects.filter(id=g_id).exists():
                raise forms.ValidationError(f"Nhóm câu hỏi với ID {g_id} không tồn tại trong hệ thống.")
        return rules_str

    def clean_dynamic_question_ids(self):
        qids_str = self.cleaned_data.get('dynamic_question_ids', '')
        if not qids_str:
            return qids_str
            
        raw_ids = re.split(r'[\s,\t\r\n]+', qids_str)
        invalid_ids = []
        cleaned_ids = []
        for rid in raw_ids:
            rid = rid.strip()
            if not rid:
                continue
            if rid.isdigit():
                cleaned_ids.append(int(rid))
            else:
                invalid_ids.append(rid)
                
        if invalid_ids:
            raise forms.ValidationError(f"Các ID câu hỏi sau không hợp lệ (phải là số): {', '.join(invalid_ids)}")
            
        if cleaned_ids:
            existing = set(Question.objects.filter(id__in=cleaned_ids).values_list('id', flat=True))
            missing = set(cleaned_ids) - existing
            if missing:
                raise forms.ValidationError(f"Các ID câu hỏi sau không tồn tại trong hệ thống: {', '.join(map(str, missing))}")
        return qids_str


@admin.register(DynamicTest)
class DynamicTestAdmin(admin.ModelAdmin):
    form = DynamicTestForm
    list_display = ['title', 'price', 'short_description', 'total_points', 'max_questions', 'start_time', 'end_time', 'is_official', 'shuffle_parts', 'creator']
    list_filter = ['is_official', 'shuffle_parts']
    search_fields = ['title']
    exclude = ['creator']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(creator=request.user)

    def save_model(self, request, obj, form, change):
        if not change or not obj.creator:
            obj.creator = request.user
        super().save_model(request, obj, form, change)


@admin.register(Classroom)
class ClassroomAdmin(admin.ModelAdmin):
    list_display = ['name', 'creator', 'invite_code', 'course', 'created_at']
    search_fields = ['name', 'creator__username']

@admin.register(ClassroomMembership)
class ClassroomMembershipAdmin(admin.ModelAdmin):
    list_display = ['classroom', 'student', 'status', 'joined_at']
    list_filter = ['status']
    search_fields = ['classroom__name', 'student__username']

@admin.register(ClassroomItem)
class ClassroomItemAdmin(admin.ModelAdmin):
    list_display = ['classroom', 'lesson', 'added_at']
    list_filter = ['classroom']


