/*
 * w5500_spi.h — W5500 SPI bring-up and WIZnet ioLibrary callbacks.
 */

#ifndef W5500_SPI_H
#define W5500_SPI_H

/*
 * Initialise SPI0, reset the W5500, register the ioLibrary SPI/CS callbacks,
 * and bring up the PHY. Call once at startup before launching Core 1.
 */
void w5500_spi_init(void);

#endif /* W5500_SPI_H */
