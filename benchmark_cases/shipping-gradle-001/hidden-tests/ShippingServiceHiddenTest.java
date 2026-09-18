package demo;

import org.junit.Test;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.fail;

public class ShippingServiceHiddenTest {
    private final ShippingService service = new ShippingService();

    @Test
    public void rejectsAnotherNegativeValueWithExactMessage() {
        try {
            service.shippingFee(-42);
            fail("Expected a negative subtotal to be rejected");
        } catch (IllegalArgumentException expected) {
            assertEquals("Subtotal must not be negative", expected.getMessage());
        }
    }

    @Test
    public void keepsChargingBelowThreshold() {
        assertEquals(500, service.shippingFee(6789));
    }

    @Test
    public void providesFreeShippingFarAboveThreshold() {
        assertEquals(0, service.shippingFee(50000));
    }
}
