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
- **Tự động Cập nhật**: Tùy chọn tự động tải xuống và áp dụng các bản cập nhật ngay khi được phát hiện. Tiện ích cũng sẽ **gửi một thông báo hệ thống** liệt kê danh sách các blueprint đã được cập nhật để bạn dễ dàng theo dõi.
- **Cải tiến Logic Reload**: Tự động tải lại các miền `automation`, `script`, và `template` sau khi cập nhật để đảm bảo thay đổi có hiệu lực ngay lập tức.
- **An toàn là trên hết**: Kiểm tra kỹ lưỡng blueprint trước khi cập nhật - bao gồm cú pháp YAML, cấu trúc blueprint, và tính tương thích phiên bản Home Assistant. Nếu phát hiện bất kỳ vấn đề nào (lỗi cú pháp, thiếu trường bắt buộc, hoặc phiên bản HA quá cũ), bản cập nhật sẽ bị chặn với thông báo lỗi rõ ràng để bảo vệ hệ thống của bạn.
- **Cảnh báo Tác động (Usage Insight)**: Trước khi cập nhật, tiện ích sẽ tính toán và hiển thị chính xác số lượng Automation và Script đang phụ thuộc vào blueprint này, giúp bạn lường trước mức độ ảnh hưởng của bản cập nhật.
- **Sao lưu & Phục hồi (Backup & Restore)**: Tự động tạo bản sao lưu đánh số (`.bak.1`, `.bak.2`, `.bak.3`) trước khi cập nhật blueprint, giữ lại tối đa N phiên bản cũ (có thể cấu hình, mặc định 3). Nếu bản cập nhật làm hỏng tự động hóa, bạn có thể dễ dàng khôi phục bất kỳ phiên bản cũ nào.
- **Cập nhật hàng loạt (Update All)**: Cập nhật đồng loạt tất cả các blueprint đang có bản mới thông qua dịch vụ `blueprints_updater.update_all` cực kỳ nhanh chóng mà không làm treo Home Assistant.
- **Làm mới thủ công (Manual Refresh)**: Kích hoạt quét ngay lập tức thông qua dịch vụ **`blueprints_updater.reload`** trong Developer Tools.
- **Tự động nhận diện (Dynamic Discovery)**: Tự động phát hiện và thêm các blueprint mới dưới dạng thực thể cập nhật mà không cần khởi động lại.
- **Hỗ trợ đa ngôn ngữ (Multilingual)**: Được địa phương hóa hoàn toàn cho nhiều ngôn ngữ khác nhau. Tích hợp tự động thích ứng với cài đặt ngôn ngữ trong Home Assistant của bạn.

---

## Cài đặt

### Tùy chọn 1: Sử dụng HACS (Khuyến nghị)

[![Add Blueprints Updater to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luuquangvu&repository=blueprints-updater&category=integration)

1.  Mở **HACS** trong Home Assistant.
2.  Tìm kiếm **Blueprints Updater**.
3.  Nếu không tìm thấy, nhấp vào biểu tượng ba chấm ở góc trên cùng bên phải và chọn **Kho lưu trữ tùy chỉnh (Custom repositories)**.
4.  Thêm `https://github.com/luuquangvu/blueprints-updater` với danh mục **Bộ tích hợp (Integration)**.
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
3.  **Bật Tự động Cập nhật**: (Tùy chọn) Nếu được bật, các blueprint phù hợp với tiêu chí của bạn sẽ được cập nhật tự động mà không cần can thiệp thủ công. **Một thông báo hệ thống sẽ xuất hiện** sau mỗi lần tự động cập nhật thành công để bạn biết những gì đã thay đổi.
4.  Chọn **Khoảng thời gian cập nhật (Update Interval)** (mặc định là 24 giờ).
5.  Chọn **Chế độ lọc (Filter Mode)**:
    - **Cập nhật tất cả (Update All)**: Theo dõi tất cả blueprint tìm thấy trong thư mục của bạn.
    - **Danh sách trắng (Whitelist)**: Chỉ theo dõi các blueprint bạn chọn cụ thể từ danh sách.
    - **Danh sách đen (Blacklist)**: Theo dõi tất cả các blueprint _ngoại trừ_ những cái bạn chọn.
6.  Sau khi thêm, tích hợp sẽ quét các blueprint của bạn. Nếu tìm thấy bản cập nhật, chúng sẽ xuất hiện dưới dạng thực thể `update` trong bảng điều khiển của bạn.

### Sao lưu & Phục hồi (Backup & Restore)

Blueprints Updater cung cấp một mạng lưới an toàn tích hợp sẵn, cho phép bạn sao lưu các blueprint trước khi chúng được cập nhật và khôi phục chúng nếu cần thiết.

#### Kích hoạt Sao lưu

Khi cài đặt bản cập nhật từ bảng điều khiển Home Assistant, bạn sẽ có tùy chọn tích chọn **Backup (Sao lưu)**. Nếu được bật, tiện ích sẽ tự động lưu blueprint hiện tại của bạn thành một tệp sao lưu có đánh số (`.bak.1`, `.bak.2`, v.v.) trước khi ghi đè bằng phiên bản mới.

> **Lưu ý:** Nếu bạn đang bật tùy chọn **Tự động Cập nhật (Auto-Update)**, tiện ích sẽ **luôn luôn** tự động sao lưu cấu hình trước khi ghi đè bản mới nhất, tạo ra một mạng lưới an toàn 100% giúp bạn hoàn toàn yên tâm.

#### Khôi phục bản Sao lưu

Nếu bạn phát hiện ra rằng bản blueprint mới cập nhật làm hỏng các automations hoặc có thay đổi không tương thích, bạn có thể dễ dàng quay về phiên bản trước đó:

1. Đi tới **Công cụ nhà phát triển (Developer Tools)** > **Hành động (Actions)**.
2. Tìm kiếm hành động **`blueprints_updater.restore_blueprint`**.
3. Chọn thực thể `update` tương ứng với blueprint mà bạn muốn khôi phục.
4. (Tùy chọn) Nhập **Backup Version** mà bạn muốn khôi phục (mặc định là **1** cho bản sao lưu gần nhất).
5. Nhấn **Thực hiện hành động (Perform Action)**.

Tiện ích sẽ tự động tìm tệp sao lưu tương ứng, khôi phục lại nội dung YAML gốc, và tự động tải lại (reload) các automations và scripts để áp dụng các thay đổi ngay lập tức.

### Áp dụng thay đổi (Thêm/Xóa Blueprints)

Vì Home Assistant không liên tục giám sát tệp hệ thống để tiết kiệm tài nguyên, nên **việc thêm hoặc xóa blueprint sẽ không được cập nhật ngay lập tức**.

Để áp dụng các thay đổi này tức thì mà không cần chờ đến lần quét tự động tiếp theo, bạn phải làm **một** trong các cách sau:

1. **Chạy hành động Reload (Khuyên dùng)**: Vào **Công cụ nhà phát triển (Developer Tools)** > **Hành động (Actions)**, tìm kiếm hành động **`blueprints_updater.reload`** và nhấn **Thực hiện hành động (Perform Action)**. Tích hợp sẽ ngay lập tức quét thư mục blueprints và sinh ra/xóa bỏ thực thể tương ứng.
2. **Reload Tích hợp**: Vào **Cài đặt (Settings)** > **Thiết bị & Dịch vụ (Devices & Services)** > **Blueprints Updater**, nhấn vào ba chấm và chọn **Tải lại (Reload)**.
3. **Khởi động lại Home Assistant**.

### Thử nghiệm tính năng

Nếu bạn muốn thấy quy trình cập nhật hoạt động như thế nào ngay lập tức, bạn có thể sử dụng bản thiết kế **Motion-Activated Light/Switch (Daily Update)**. Bản thiết kế này được cập nhật tự động hàng ngày thông qua GitHub Actions để mô phỏng một bản phát hành mới.

**Cài đặt nhanh:**
[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fluuquangvu%2Fdemo-blueprints%2Fblob%2Fmain%2Fblueprints%2Fmotion_light_blueprint.yaml)

**Cài đặt thủ công:**

1.  Sao chép URL này: `https://github.com/luuquangvu/demo-blueprints/blob/main/blueprints/motion_light_blueprint.yaml`
2.  Trong Home Assistant, đi tới **Cài đặt** > **Tự động hóa & Cảnh** > **Bản thiết kế**.
3.  Nhấp vào **Nhập bản thiết kế** và dán URL vào.

Sau khi nhập xong, **Blueprints Updater** sẽ tự động phát hiện nó trong lần quét định kỳ tiếp theo. Để thấy kết quả ngay lập tức, bạn có thể [kích hoạt quét thủ công](#áp-dụng-thay-đổi-thêmxóa-blueprints). Khi GitHub Action cập nhật bản thiết kế, bạn sẽ nhận được thông báo trong Home Assistant và có thể thực hiện cập nhật.

---

## Yêu cầu

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
