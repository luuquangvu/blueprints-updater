# Blueprints Updater cho Home Assistant

**[ [🇺🇸 English](README.md) | 🇻🇳 Tiếng Việt ]**

**Blueprints Updater** là một tích hợp cho Home Assistant giúp bạn tự động theo dõi và cập nhật các blueprint đã cài đặt (**Automations**, **Scripts**, và **Template Entities**) từ URL nguồn ban đầu. Nó hoạt động giống như một thực thể cập nhật (update entity) gốc, cung cấp thông báo và nút cập nhật bằng một cú nhấp chuột khi phát hiện phiên bản mới.

---

## Tính năng

- **Quét định kỳ tự động**: Quét thư mục `blueprints/` cục bộ sau mỗi X giờ (có thể cấu hình).
- **So sánh thông minh**: Sử dụng băm **SHA256** để so sánh nội dung tệp cục bộ với phiên bản mới nhất từ URL nguồn.
- **Hỗ trợ URL nâng cao**:
  - **GitHub**: Tự động chuyển đổi URL blob tiêu chuẩn sang URL tệp raw.
  - **Gist**: Hỗ trợ GitHub Gist gốc.
  - **HA Community Forum**: Phân tích trực tiếp các chủ đề trên diễn đàn để trích xuất mã blueprint YAML mới nhất.
- **Duy trì nguồn**: Tự động đảm bảo thẻ `source_url` được giữ lại trong các tệp đã cập nhật để các bản cập nhật trong tương lai luôn hoạt động.
- **Lọc nâng cao (Advanced Filtering)**: Chọn cập nhật tất cả blueprint, hoặc chỉ những blueprint cụ thể (**Whitelist**), hoặc loại trừ các blueprint cụ thể (**Blacklist**).
- **Tự động Cập nhật**: Tùy chọn tự động tải xuống và áp dụng các bản cập nhật ngay khi được phát hiện.
- **Cải tiến Logic Reload**: Tự động tải lại các miền `automation`, `script`, và `template` sau khi cập nhật để đảm bảo thay đổi có hiệu lực ngay lập tức.
- **An toàn là trên hết**: Kiểm tra tính hợp lệ của mã YAML trước khi cập nhật. Nếu tệp nguồn bị lỗi cú pháp, hệ thống sẽ chặn cập nhật để bảo vệ cấu hình của bạn và cung cấp thông báo lỗi chi tiết.
- **Làm mới thủ công (Manual Refresh)**: Kích hoạt quét ngay lập tức thông qua dịch vụ **`blueprints_updater.reload`** trong Developer Tools.
- **Tự động nhận diện (Dynamic Discovery)**: Tự động phát hiện và thêm các blueprint mới dưới dạng thực thể cập nhật mà không cần khởi động lại.

---

## Cài đặt

### Tùy chọn 1: Sử dụng HACS (Khuyến nghị)

[![Add Blueprints Updater to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luuquangvu&repository=blueprints_updater&category=integration)

1.  Mở **HACS** trong Home Assistant.
2.  Tìm kiếm **Blueprints Updater**.
3.  Nếu không tìm thấy, nhấp vào biểu tượng ba chấm ở góc trên cùng bên phải và chọn **Kho lưu trữ tùy chỉnh (Custom repositories)**.
4.  Thêm `https://github.com/luuquangvu/blueprints_updater` với danh mục **Bộ tích hợp (Integration)**.
5.  Tìm kiếm **Blueprints Updater** và nhấp vào **Tải xuống (Download)**.
6.  Khởi động lại Home Assistant.

### Tùy chọn 2: Cài đặt thủ công

1.  Tải bản phát hành mới nhất và giải nén các tệp.
2.  Sao chép thư mục `custom_components/blueprints_updater` vào thư mục `config/custom_components/` của Home Assistant.
3.  Khởi động lại Home Assistant.

---

## Thiết lập & Cấu hình

1.  Đi tới **Cài đặt (Settings)** > **Thiết bị & Dịch vụ (Devices & Services)**.
2.  Nhấp vào **Thêm bộ tích hợp (Add Integration)** và tìm kiếm **Blueprints Updater**.
3.  **Bật Tự động Cập nhật**: (Tùy chọn) Nếu được bật, các blueprint phù hợp với tiêu chí của bạn sẽ được cập nhật tự động mà không cần can thiệp thủ công.
4.  Chọn **Khoảng thời gian cập nhật (Update Interval)** (mặc định là 24 giờ).
5.  Chọn **Chế độ lọc (Filter Mode)**:
    - **Cập nhật tất cả (Update All)**: Theo dõi tất cả blueprint tìm thấy trong thư mục của bạn.
    - **Danh sách trắng (Whitelist)**: Chỉ theo dõi các blueprint bạn chọn cụ thể từ danh sách.
    - **Danh sách đen (Blacklist)**: Theo dõi tất cả các blueprint _ngoại trừ_ những cái bạn chọn.
6.  Sau khi thêm, tích hợp sẽ quét các blueprint của bạn. Nếu tìm thấy bản cập nhật, chúng sẽ xuất hiện dưới dạng thực thể `update` trong bảng điều khiển của bạn.

### Làm mới thủ công (Hot Reload)

Nếu bạn vừa thêm một tệp blueprint mới hoặc muốn kiểm tra bản cập nhật ngay lập tức mà không muốn chờ đợi:

1.  Vào **Công cụ nhà phát triển (Developer Tools)** > thẻ **Hành động (Actions)**.
2.  Tìm kiếm hành động **`blueprints_updater.reload`**.
3.  Nhấp vào **Thực hiện hành động (Perform Action)**.
    Tích hợp sẽ ngay lập tức quét thư mục blueprints và tạo thêm các thực thể mới hoặc cập nhật trạng thái nếu có.

### Yêu cầu

Để một blueprint có thể theo dõi được, nó **phải** chứa một `source_url` hợp lệ trong siêu dữ liệu:

```yaml
blueprint:
  name: "Tên Blueprint"
  source_url: https://github.com/user/repo/blob/main/blueprint.yaml
  # ...
```

---

## Đóng góp

Mọi đóng góp đều được chào đón! Nếu bạn tìm thấy lỗi hoặc có yêu cầu tính năng, vui lòng tạo issue hoặc gửi pull request.

## Bản quyền

Dự án này được cấp phép theo Giấy phép MIT.
