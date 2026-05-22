from django.core.management.base import BaseCommand
from django.db import transaction, models
from django.db.models import Q
from django.apps import apps

class Command(BaseCommand):
    help = 'Chuyển đổi toàn bộ dấu nháy kép Word/Smart (“, ”) thành dấu nháy kép tiêu chuẩn (") trong cơ sở dữ liệu'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Chỉ hiển thị các thay đổi dự kiến mà không thực sự cập nhật cơ sở dữ liệu',
        )
        parser.add_argument(
            '--all-apps',
            action='store_true',
            help='Quét tất cả các ứng dụng đã cài đặt thay vì chỉ ứng dụng lms',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        all_apps = options['all_apps']

        if dry_run:
            self.stdout.write(self.style.WARNING("--- ĐANG CHẠY Ở CHẾ ĐỘ THỬ NGHIỆM (DRY-RUN) - KHÔNG GHI VÀO CSDL ---"))
        else:
            self.stdout.write(self.style.WARNING("--- ĐANG THỰC HIỆN CẬP NHẬT CƠ SỞ DỮ LIỆU ---"))

        # Bản đồ thay thế các loại dấu nháy cong/dưới sang dấu nháy chuẩn
        replacements = {
            '“': '"',  # U+201C: Left double quotation mark
            '”': '"',  # U+201D: Right double quotation mark
            '„': '"',  # U+201E: Double low-9 quotation mark
            '‘': "'",  # U+2018: Left single quotation mark
            '’': "'",  # U+2019: Right single quotation mark
            '‚': "'",  # U+201A: Single low-9 quotation mark
        }

        total_updated_fields = 0
        total_updated_records = 0

        # Lọc models cần quét
        if all_apps:
            # Bỏ qua các app hệ thống nhạy cảm của Django nếu quét tất cả
            ignored_apps = {'admin', 'contenttypes', 'sessions', 'messages', 'staticfiles'}
            models_to_scan = [
                m for m in apps.get_models() 
                if m._meta.app_label not in ignored_apps
            ]
            self.stdout.write("Đang quét tất cả các ứng dụng trong hệ thống...")
        else:
            models_to_scan = [m for m in apps.get_models() if m._meta.app_label == 'lms']
            self.stdout.write("Đang quét ứng dụng chính: lms...")

        for model in models_to_scan:
            # Lọc các trường CharField và TextField có thể chứa ký tự
            text_fields = [
                field for field in model._meta.get_fields()
                if isinstance(field, (models.CharField, models.TextField))
            ]

            if not text_fields:
                continue

            for field in text_fields:
                field_name = field.name
                
                # Tạo điều kiện truy vấn Q để tìm tất cả các trường chứa ít nhất một ký tự trong danh sách replacements
                q_objects = Q()
                for char in replacements.keys():
                    q_objects |= Q(**{f"{field_name}__contains": char})

                # Truy vấn các bản ghi khớp
                queryset = model.objects.filter(q_objects)
                count = queryset.count()

                if count > 0:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Phát hiện {count} bản ghi chứa ký tự nháy Word trong [{model._meta.app_label}.{model.__name__}] ở trường '{field_name}'"
                        )
                    )
                    
                    updated_in_field = 0
                    
                    # Sử dụng transaction để tối ưu hóa và đảm bảo an toàn dữ liệu
                    with transaction.atomic():
                        for obj in queryset:
                            val = getattr(obj, field_name)
                            if val:
                                new_val = val
                                for src, dest in replacements.items():
                                    new_val = new_val.replace(src, dest)
                                
                                if val != new_val:
                                    if not dry_run:
                                        setattr(obj, field_name, new_val)
                                        obj.save(update_fields=[field_name])
                                    updated_in_field += 1
                                    
                    if updated_in_field > 0:
                        total_updated_fields += 1
                        total_updated_records += updated_in_field
                        action_str = "Sẽ cập nhật" if dry_run else "Đã cập nhật"
                        self.stdout.write(f"  -> {action_str} {updated_in_field} bản ghi trong {model.__name__}.{field_name}")

        # In kết quả tổng quan
        self.stdout.write("\n" + "="*50)
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[Dry-Run Hoàn tất] Dự kiến sẽ sửa đổi {total_updated_records} bản ghi thuộc {total_updated_fields} trường khác nhau."
                )
            )
            self.stdout.write(self.style.WARNING("Lưu ý: Chưa có dữ liệu nào thực sự bị thay đổi trong CSDL."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[Hoàn tất] Đã sửa đổi thành công {total_updated_records} bản ghi thuộc {total_updated_fields} trường khác nhau trong CSDL."
                )
            )
        self.stdout.write("="*50 + "\n")
