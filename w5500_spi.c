

#include "w5500_spi.h"

#include <stdio.h>
#include "pico/time.h"    /* sleep_ms */
#include "hardware/spi.h"   /* spi_init, spi_read_blocking, spi_write_blocking */
#include "hardware/gpio.h"  /* gpio_init, gpio_set_dir, gpio_put */

#include "wizchip_conf.h"   /* reg_wizchip_cs_cbfunc, reg_wizchip_spi_cbfunc, wizchip_init */
#define W5500_SPI_PORT      spi0        /* We use SPI0 (the Pico has SPI0 and SPI1) */
#define W5500_SPI_SPEED_HZ  5000000     /* 5 MHz — reliable speed for jumper wires  */

#define W5500_PIN_MISO      16  /* GP16: SPI0 MISO — W5500 sends data to Pico    */
#define W5500_PIN_CS        17  /* GP17: Chip Select — we control manually        */
#define W5500_PIN_SCK       18  /* GP18: SPI0 Clock — synchronizes data transfer  */
#define W5500_PIN_MOSI      19  /* GP19: SPI0 MOSI — Pico sends data to W5500     */
#define W5500_PIN_RST       20  /* GP20: Reset — pull LOW then HIGH to reset W5500 */

static void w5500_cs_select(void) {
    asm volatile("nop \n nop \n nop");  /* Tiny setup delay (~ns) */
    gpio_put(W5500_PIN_CS, 0);          /* CS = LOW = chip selected */
    asm volatile("nop \n nop \n nop");
}


static void w5500_cs_deselect(void) {
    asm volatile("nop \n nop \n nop");
    gpio_put(W5500_PIN_CS, 1);          /* CS = HIGH = chip deselected */
    asm volatile("nop \n nop \n nop");
}


static uint8_t w5500_spi_readbyte(void) {
    uint8_t rx_data = 0;
    spi_read_blocking(W5500_SPI_PORT, 0xFF, &rx_data, 1);                         
    return rx_data;
}


static void w5500_spi_writebyte(uint8_t data) {
    spi_write_blocking(W5500_SPI_PORT, &data, 1);
}


static void w5500_cris_enter(void) {  }
static void w5500_cris_exit(void)  {  }


static void w5500_spi_readburst(uint8_t *buf, uint16_t len) {
    spi_read_blocking(W5500_SPI_PORT, 0xFF, buf, len);
}


static void w5500_spi_writeburst(uint8_t *buf, uint16_t len) {
    spi_write_blocking(W5500_SPI_PORT, buf, len);
}



void w5500_spi_init(void) {

    spi_init(W5500_SPI_PORT, W5500_SPI_SPEED_HZ);

    spi_set_format(W5500_SPI_PORT, 8, SPI_CPOL_0, SPI_CPHA_0, SPI_MSB_FIRST);

    
    gpio_set_function(W5500_PIN_MISO, GPIO_FUNC_SPI);
    gpio_set_function(W5500_PIN_SCK,  GPIO_FUNC_SPI);
    gpio_set_function(W5500_PIN_MOSI, GPIO_FUNC_SPI);


    gpio_init(W5500_PIN_CS);
    gpio_set_dir(W5500_PIN_CS, GPIO_OUT);
    gpio_put(W5500_PIN_CS, 1);   /* Start HIGH = chip NOT selected */


    gpio_init(W5500_PIN_RST);
    gpio_set_dir(W5500_PIN_RST, GPIO_OUT);

    gpio_put(W5500_PIN_RST, 0);  /* Pull RESET LOW — chip enters reset state */
    sleep_ms(10);                /* Hold for 10 ms (datasheet: minimum 2 µs)  */
    gpio_put(W5500_PIN_RST, 1);  /* Release RESET — chip starts booting       */
    sleep_ms(200);               /* Wait 200 ms for W5500 to fully initialize */

   
    reg_wizchip_cris_cbfunc(w5500_cris_enter, w5500_cris_exit);
    reg_wizchip_cs_cbfunc(w5500_cs_select, w5500_cs_deselect);
    reg_wizchip_spi_cbfunc(w5500_spi_readbyte, w5500_spi_writebyte);
    reg_wizchip_spiburst_cbfunc(w5500_spi_readburst, w5500_spi_writeburst);

    
    uint8_t rx_size[8] = {16, 0, 0, 0, 0, 0, 0, 0};  /* Socket 0: all 16KB RX */
    uint8_t tx_size[8] = {16, 0, 0, 0, 0, 0, 0, 0};  /* Socket 0: all 16KB TX */
    /* Signature is wizchip_init(txsize, rxsize) — pass TX first, then RX. */
    wizchip_init(tx_size, rx_size);

    printf("[W5500] SPI0 initialized at 5MHz | MOSI=GP19, MISO=GP16, SCK=GP18, CS=GP17\n");
    printf("[W5500] Hardware reset complete. W5500 ready.\n");

 
    setPHYCFGR(0x78);   /* SW mode, auto-negotiate, RST=0 */
    sleep_ms(1);
    setPHYCFGR(0xF8);   /* RST=1, start auto-negotiate */
    sleep_ms(500);      /* Auto-negotiate needs longer to settle */

    printf("[W5500] PHY: auto-negotiate enabled.\n");

   
    printf("[W5500] Waiting for Ethernet link...\n");
    for (int i = 0; i < 20; i++) {
        uint8_t phy = getPHYCFGR();
        bool link_up = (phy & 0x01);
        printf("[W5500]   PHY register = 0x%02X | Link: %s\n",
               phy, link_up ? "UP" : "DOWN");
        if (link_up) {
            printf("[W5500] Ethernet link established!\n");
            return;
        }
        sleep_ms(500);
    }
    printf("[W5500] WARNING: No Ethernet link after 10 seconds!\n");
    printf("[W5500]   Check cable and adapter.\n");
}
