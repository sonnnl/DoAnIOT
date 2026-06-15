#include <Wire.h>
#include <BH1750.h>

#define LED_LIGHT_PIN 27    // Đèn LED giả lập chiếu sáng (GPIO 27)
const float LUX_THRESHOLD = 30.0; // Ngưỡng ánh sáng để bật đèn (dưới 30 lx là tối)

BH1750 lightMeter;

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n==========================================");
  Serial.println(" TEST CAM BIEN BH1750 + LED CHIEU SANG G27 ");
  Serial.println("==========================================");

  // Cấu hình chân LED
  pinMode(LED_LIGHT_PIN, OUTPUT);
  digitalWrite(LED_LIGHT_PIN, LOW); // Tắt đèn ban đầu

  // Khoi chay I2C tren chan 21 (SDA) va 22 (SCL) cua ESP32
  Wire.begin(21, 22);
  delay(500);

  // ==========================================
  // 1. Quet I2C xem co ket noi vat ly khong
  // ==========================================
  Serial.println("1. Dang quet bus I2C...");
  byte error, address;
  int devicesCount = 0;
  byte foundAddress = 0;

  for (address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    error = Wire.endTransmission();

    if (error == 0) {
      Serial.print("   -> Tim thay thiet bi tai dia chi: 0x");
      if (address < 16) Serial.print("0");
      Serial.print(address, HEX);
      Serial.println(" !");
      
      // Ghi lai neu gap dia chi cua BH1750
      if (address == 0x23 || address == 0x5C) {
        foundAddress = address;
      }
      devicesCount++;
    }
  }

  if (devicesCount == 0) {
    Serial.println("   -> [LOI] Khong thay thiet bi nao tren bus I2C! Kiem tra lai day SCL/SDA.");
  } else {
    Serial.print("   -> Quet xong. Tim thay "); 
    Serial.print(devicesCount); 
    Serial.println(" thiet bi.");
  }

  // ==========================================
  // 2. Khoi dong cam bien voi dia chi phu hop
  // ==========================================
  Serial.println("\n2. Dang khoi dong cam bien BH1750...");
  
  if (foundAddress != 0) {
    if (lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE, foundAddress)) {
      Serial.print("   -> [OK] BH1750 khoi dong thanh cong tai dia chi: 0x");
      Serial.println(foundAddress, HEX);
    } else {
      Serial.println("   -> [LOI] Co thiet bi o dia chi do nhung khoi dong BH1750 that bai!");
    }
  } else {
    Serial.println("   -> [LOI] Khong do ra dia chi 0x23 hoac 0x5C cua cam bien BH1750.");
    Serial.println("      Vui long kiem tra nguon 3.3V va GND cua GY-302.");
  }
  Serial.println("==========================================\n");
}

void loop() {
  // Doc cuong do anh sang
  float lux = lightMeter.readLightLevel();
  
  Serial.print("Anh sang do duoc: ");
  if (lux >= 0) {
    Serial.print(lux);
    Serial.print(" lx");
    
    // Điều khiển đèn LED dựa trên độ sáng
    if (lux < LUX_THRESHOLD) {
      digitalWrite(LED_LIGHT_PIN, HIGH);
      Serial.println(" -> [LED G27: BAT] (Troi toi)");
    } else {
      digitalWrite(LED_LIGHT_PIN, LOW);
      Serial.println(" -> [LED G27: TAT] (Troi sang)");
    }
  } else {
    Serial.println("LOI (Chua khoi dong hoac mat ket noi)");
    digitalWrite(LED_LIGHT_PIN, LOW); // Đảm bảo tắt LED khi lỗi cảm biến
  }
  
  delay(1000); // Doc lai sau moi 1 giay
}
