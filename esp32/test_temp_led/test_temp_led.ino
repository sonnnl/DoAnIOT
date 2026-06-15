#include <DHT.h>

// Cấu hình chân kết nối
#define DHTPIN 23        // Chân DATA của cảm biến DHT (đang dùng ở file test_lcd_oled.ino)
#define DHTTYPE DHT22    // Loại cảm biến (DHT11 hoặc DHT22)
#define LED_PIN 26       // KHÔNG DÙNG G34 (Input only). Hãy cắm chân + của LED vào GPIO 26

DHT dht(DHTPIN, DHTTYPE);

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("--- TEST NHIỆT ĐỘ > 30 ĐỘ BẬT ĐÈN ---");

  // Khởi tạo cảm biến DHT
  dht.begin();

  // Khởi tạo chân LED là OUTPUT
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW); // Tắt đèn ban đầu
}

void loop() {
  // Đọc nhiệt độ từ DHT
  float t = dht.readTemperature();

  // Kiểm tra lỗi đọc cảm biến
  if (isnan(t)) {
    Serial.println("Lỗi: Không đọc được dữ liệu từ cảm biến DHT!");
    // Nhấp nháy đèn báo lỗi nếu cần
    digitalWrite(LED_PIN, HIGH);
    delay(100);
    digitalWrite(LED_PIN, LOW);
    delay(100);
    return;
  }

  Serial.print("Nhiệt độ hiện tại: ");
  Serial.print(t, 1);
  Serial.println("°C");

  // Kiểm tra điều kiện nhiệt độ > 30 độ C
  if (t > 31.6) {
    digitalWrite(LED_PIN, HIGH); // Bật đèn
    Serial.println("-> Nhiệt độ > 30°C: BẬT ĐÈN!");
  } else {
    digitalWrite(LED_PIN, LOW);  // Tắt đèn
    Serial.println("-> Nhiệt độ <= 30°C: TẮT ĐÈN.");
  }

  delay(2000); // Đọc lại sau mỗi 2 giây
}
