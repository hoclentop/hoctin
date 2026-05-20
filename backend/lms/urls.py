from django.urls import path, reverse_lazy
from django.views.generic import TemplateView
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', views.course_list, name='course_list'),
    path('course/<int:course_id>/', views.course_detail, name='course_detail'),
    path('tests/', views.test_list, name='test_list'),
    path('test/<int:test_id>/', views.test_detail, name='test_detail'),
    path('test/<int:test_id>/register/', views.register_test, name='register_test'),
    path('test/<int:test_id>/checkout/', views.test_checkout, name='test_checkout'),
    path('test/<int:test_id>/take/', views.take_test, name='take_test'),
    path('attempt/<int:attempt_id>/submit/', views.submit_test, name='submit_test'),
    path('result/<int:attempt_id>/', views.test_result, name='test_result'),
    path('result/<int:attempt_id>/review/', views.review_attempt, name='review_attempt'),
    path('test/<int:test_id>/leaderboard/', views.leaderboard, name='leaderboard'),
    path('wallet/deposit/', views.wallet_deposit, name='wallet_deposit'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    path('password-change/', auth_views.PasswordChangeView.as_view(
        template_name='registration/password_change.html',
        form_class=views.StyledPasswordChangeForm,
        success_url=reverse_lazy('password_change_done')
    ), name='password_change'),
    path('password-change/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='registration/password_change_done.html'
    ), name='password_change_done'),
    path('register/', views.register, name='register'),
    path('upload-image/', views.upload_image, name='upload_image'),
    path('editor-demo/', TemplateView.as_view(template_name='lms/editor_demo.html'), name='editor_demo'),
    path('attempts/', views.attempt_history, name='attempt_history'),
    path('teacher/attempts/', views.teacher_attempts, name='teacher_attempts'),
    path('attempt/<int:attempt_id>/delete/', views.delete_attempt, name='delete_attempt'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-dashboard/toggle/<int:user_id>/<str:perm_type>/', views.toggle_staff_permission, name='toggle_staff_permission'),
    path('admin-dashboard/transactions/', views.admin_transactions, name='admin_transactions'),
    path('admin-dashboard/transactions/<int:tx_id>/approve/', views.approve_transaction_admin, name='approve_transaction_admin'),
    path('admin-dashboard/transactions/<int:tx_id>/reject/', views.reject_transaction_admin, name='reject_transaction_admin'),
    path('questions/create/', views.create_question, name='create_question'),
    path('questions/<int:pk>/edit/', views.edit_question, name='edit_question'),
    path('questions/', views.question_list, name='question_list'),
    path('questions/<int:pk>/delete/', views.delete_question, name='delete_question'),
    path('questions/<int:pk>/', views.question_detail, name='question_detail'),
    path('questions/<int:pk>/duplicate/', views.duplicate_question, name='duplicate_question'),
    path('questions/<int:pk>/preview/', views.preview_question, name='preview_question'),
    path('questions/<int:pk>/check/', views.check_question_answer, name='check_question_answer'),
    path('questions/groups/create-ajax/', views.create_group_ajax, name='create_group_ajax'),
    path('questions/bulk-assign-group/', views.bulk_assign_group_ajax, name='bulk_assign_group_ajax'),
    path('questions/import/', views.question_import, name='question_import'),
    path('questions/import/parse-ajax/', views.parse_import_ajax, name='parse_import_ajax'),
    path('questions/import/save-ajax/', views.save_import_ajax, name='save_import_ajax'),
    path('questions/import/template/', views.download_csv_template, name='download_csv_template'),
    
    # URL tạo đề thi & Quản lý cấu trúc
    path('tests/create/', views.create_test, name='create_test'),
    path('tests/<int:test_id>/edit-basic/', views.edit_test_basic, name='edit_test_basic'),
    path('tests/<int:test_id>/structure/', views.manage_test_structure, name='manage_test_structure'),
    path('tests/<int:test_id>/preview/', views.preview_test, name='preview_test'),
    path('tests/<int:test_id>/duplicate/', views.duplicate_test, name='duplicate_test'),
    path('questions/search-ajax/', views.search_questions_ajax, name='search_questions_ajax'),
    path('tests/<int:test_id>/parts/add-ajax/', views.add_test_part_ajax, name='add_test_part_ajax'),
    path('tests/parts/<int:part_id>/delete-ajax/', views.delete_test_part_ajax, name='delete_test_part_ajax'),
    path('tests/<int:test_id>/questions/add-ajax/', views.add_test_question_ajax, name='add_test_question_ajax'),
    path('tests/questions/<int:tq_id>/delete-ajax/', views.delete_test_question_ajax, name='delete_test_question_ajax'),
    path('tests/questions/<int:tq_id>/update-ajax/', views.update_test_question_ajax, name='update_test_question_ajax'),
    path('tests/<int:test_id>/questions/add-quick-ajax/', views.add_questions_quick_ajax, name='add_questions_quick_ajax'),
    path('tests/<int:test_id>/instructions/update-ajax/', views.update_part_instruction_ajax, name='update_part_instruction_ajax'),
    
    # URL Gói đề luyện tập
    path('bundles/', views.test_bundle_list, name='test_bundle_list'),
    path('bundle/<int:bundle_id>/', views.test_bundle_detail, name='test_bundle_detail'),
    path('bundle/<int:bundle_id>/buy/', views.buy_test_bundle, name='buy_test_bundle'),
    path('bundle/<int:bundle_id>/checkout/', views.test_bundle_checkout, name='test_bundle_checkout'),
    
    # URL Gói khóa học
    path('course-bundles/', views.course_bundle_list, name='course_bundle_list'),
    path('course-bundle/<int:bundle_id>/', views.course_bundle_detail, name='course_bundle_detail'),
    path('course-bundle/<int:bundle_id>/buy/', views.buy_course_bundle, name='buy_course_bundle'),
    path('course-bundle/<int:bundle_id>/checkout/', views.course_bundle_checkout, name='course_bundle_checkout'),
    path('courses/<int:course_id>/checkout/', views.course_checkout, name='course_checkout'),
    
    # URL quản lý khóa học (Giáo viên)
    path('courses/', views.all_courses, name='all_courses'),
    path('courses/create/', views.create_course, name='create_course'),
    path('courses/<int:course_id>/edit/', views.edit_course, name='edit_course'),
    path('courses/<int:course_id>/delete/', views.delete_course, name='delete_course'),
    path('teacher/courses/progress/', views.teacher_courses_progress, name='teacher_courses_progress'),
    path('teacher/courses/<int:course_id>/progress/', views.teacher_course_detail_progress, name='teacher_course_detail_progress'),

    # URL quản lý bài học (Giáo viên)
    path('courses/<int:course_id>/structure/', views.manage_course_structure, name='manage_course_structure'),
    path('courses/<int:course_id>/lessons/create/', views.create_lesson, name='create_lesson'),
    path('lessons/<int:lesson_id>/edit/', views.edit_lesson, name='edit_lesson'),
    path('lessons/<int:lesson_id>/delete/', views.delete_lesson, name='delete_lesson'),

    # URL luồng học sinh
    path('courses/<int:course_id>/buy/', views.buy_course, name='buy_course'),
    path('courses/<int:course_id>/lessons/<int:lesson_id>/', views.lesson_detail, name='lesson_detail'),
    path('lessons/<int:lesson_id>/sync-progress-ajax/', views.sync_lesson_progress_ajax, name='sync_lesson_progress_ajax'),
    path('lessons/<int:lesson_id>/complete-ajax/', views.complete_lesson_ajax, name='complete_lesson_ajax'),
    
    # URL cho học sinh theo dõi khóa học & đề thi của mình
    path('my-courses/', views.my_courses, name='my_courses'),
    path('my-tests/', views.my_tests, name='my_tests'),
]
